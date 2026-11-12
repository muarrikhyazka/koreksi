"""
Koreksi — OCR + auto-grading for handwritten student answer sheets.

Design notes (read before extending):
- One exam has N questions, each independently typed 'mc' or 'essay' by the
  teacher (per-question, not per-exam — a real test mixes both).
- One upload = one student submission. Multiple files can be uploaded in a
  single request ("run multiple" == batch grade a whole class at once).
- PDF submissions get rasterized per-page (poppler `pdftoppm`) and ALL pages
  of one file are sent to the vision model together, so a 2-page answer
  sheet is read as one document, not two unrelated images.
- Grading pipeline is fully server-side, synchronous, per-file: OCR extract
  -> match to question list -> score. Kept synchronous (no job queue) since
  a class of ~40 sheets at a few seconds each is well under request
  timeouts; ponytail: move to a background task queue if batches regularly
  exceed ~60 students.
"""
import json, os, re, sqlite3, subprocess, tempfile, time, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, redirect, send_from_directory
import urllib.request

app = Flask(__name__)
WIB = timezone(timedelta(hours=7))

BASE_DIR = os.path.dirname(__file__)
DB = os.environ.get('KOREKSI_DB') or os.path.join(BASE_DIR, 'koreksi.db')
UPLOAD_DIR = os.environ.get('KOREKSI_UPLOADS') or os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

TUTOR_KEY = os.environ.get('TUTOR_KEY', '')
TUTOR_BASE = os.environ.get('TUTOR_BASE', 'http://9router:20128/api/v1')
TUTOR_MODEL = os.environ.get('TUTOR_MODEL', 'ds/deepseek-v4-flash-vision-exp')

PREFIX = os.environ.get('SCRIPT_NAME', '').rstrip('/')
if PREFIX:
    app.config['APPLICATION_ROOT'] = PREFIX


# ─── DB ───
def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            number INTEGER NOT NULL,
            type TEXT NOT NULL,            -- 'mc' | 'essay'
            prompt TEXT DEFAULT '',
            choices TEXT DEFAULT '',       -- mc: "A) ...|B) ...|..." free text, informational only
            correct_answer TEXT DEFAULT '',-- mc: letter/short answer
            model_answer TEXT DEFAULT '',  -- essay: ideal answer / rubric points
            max_score REAL NOT NULL DEFAULT 1
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_name TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            image_paths TEXT DEFAULT '',   -- JSON list, relative to uploads/
            status TEXT DEFAULT 'pending', -- pending|ocr_failed|graded
            error TEXT DEFAULT '',
            total_score REAL DEFAULT 0,
            max_score REAL DEFAULT 0,
            created_at TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            question_number INTEGER NOT NULL,
            student_answer TEXT DEFAULT '',
            score REAL DEFAULT 0,
            max_score REAL DEFAULT 0,
            feedback TEXT DEFAULT '',
            graded_by TEXT DEFAULT 'auto'  -- auto|manual
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_q_exam ON questions(exam_id, number)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_sub_exam ON submissions(exam_id)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_ans_sub ON answers(submission_id)')


init_db()


def now():
    return datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S')


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


# ─── LLM call (vision + text), tolerant of SSE-shaped bodies some
# gateway models return even for non-streaming requests ───
def llm_call(content, max_tokens=1500, model=None):
    payload = {'model': model or TUTOR_MODEL,
               'messages': [{'role': 'user', 'content': content}],
               'max_tokens': max_tokens}
    req = urllib.request.Request(
        f'{TUTOR_BASE}/chat/completions',
        data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {TUTOR_KEY}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode('utf-8', 'replace')
    # Gateway sometimes returns a single plain JSON object with a stray
    # "data: [DONE]" suffix glued on with no newline (not real SSE) — strip
    # that before falling back to real line-by-line SSE parsing.
    plain = re.sub(r'\s*data:\s*\[DONE\]\s*$', '', raw.strip())
    try:
        j = json.loads(plain)
        ch = j['choices'][0]
        return ((ch.get('message') or {}).get('content') or
                (ch.get('delta') or {}).get('content') or '')
    except Exception:
        pass
    out = ''
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        body = line[5:].strip() if line.startswith('data:') else line
        if body == '[DONE]' or not body.startswith('{'):
            continue
        try:
            j = json.loads(body)
            ch = j['choices'][0]
            out += ((ch.get('delta') or {}).get('content') or
                    (ch.get('message') or {}).get('content') or '')
        except Exception:
            continue
    return out


def extract_json(text):
    """Model sometimes wraps JSON in prose/fences — pull the first {...} block."""
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise ValueError('no JSON object in model output: ' + text[:200])
    return json.loads(m.group(0))


def image_to_data_url(path):
    import base64
    ext = os.path.splitext(path)[1].lstrip('.').lower() or 'png'
    mime = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    return f'data:{mime};base64,{b64}'


def pdf_to_images(pdf_path, out_dir):
    prefix = os.path.join(out_dir, 'page')
    subprocess.run(['pdftoppm', '-png', '-r', '150', pdf_path, prefix],
                    check=True, timeout=60)
    pages = sorted(f for f in os.listdir(out_dir) if f.startswith('page') and f.endswith('.png'))
    return [os.path.join(out_dir, p) for p in pages]


# ─── Grading ───
def ocr_submission(image_paths, questions):
    """One vision call: transcribe student name + every question's answer."""
    q_lines = []
    for q in questions:
        if q['type'] == 'mc':
            q_lines.append(f"Q{q['number']} (multiple choice): expect a single letter/short answer")
        else:
            q_lines.append(f"Q{q['number']} (essay): expect free-text written answer, transcribe fully")
    prompt = (
        "You are transcribing a scanned/photographed student answer sheet. "
        "Read the handwriting carefully. Questions on this sheet:\n" + "\n".join(q_lines) +
        "\n\nReturn ONLY JSON, no prose, no markdown fences:\n"
        '{"student_name": "...", "answers": [{"q": <number>, "answer": "<transcribed text, '
        'empty string if the question was not answered or not found>"}]}\n'
        "Include an entry for every question number listed above, even if unanswered."
    )
    content = [{'type': 'text', 'text': prompt}]
    for p in image_paths:
        content.append({'type': 'image_url', 'image_url': {'url': image_to_data_url(p)}})
    out = llm_call(content, max_tokens=2000)
    return extract_json(out)


def grade_essay(question, student_answer):
    prompt = (
        "You are grading one short-answer/essay exam question. Be fair but consistent; "
        "give partial credit for partially correct answers.\n\n"
        f"Question: {question['prompt'] or '(no prompt text given)'}\n"
        f"Model answer / rubric: {question['model_answer']}\n"
        f"Maximum score: {question['max_score']}\n"
        f"Student's written answer: {student_answer or '(blank — not answered)'}\n\n"
        'Return ONLY JSON: {"score": <number, 0 to max>, "feedback": "<one short sentence>"}'
    )
    try:
        out = llm_call([{'type': 'text', 'text': prompt}], max_tokens=300)
        j = extract_json(out)
        score = float(j.get('score', 0))
        score = max(0, min(score, question['max_score']))
        return score, str(j.get('feedback', ''))[:300]
    except Exception as e:
        return 0, f'auto-grading failed: {e}'


def grade_mc(question, student_answer):
    sa = (student_answer or '').strip().upper()
    sa = re.sub(r'^[^A-Z0-9]*([A-Z0-9]+).*$', r'\1', sa)  # "b)" / "B." / "b" -> "B"
    ca = (question['correct_answer'] or '').strip().upper()
    ok = bool(sa) and sa == ca
    return (question['max_score'] if ok else 0), ('Benar' if ok else f'Salah (kunci: {ca})')


def process_submission(sub_id, exam_id, image_paths):
    with db() as c:
        questions = [dict(r) for r in c.execute(
            'SELECT * FROM questions WHERE exam_id=? ORDER BY number', (exam_id,)).fetchall()]
    if not questions:
        with db() as c:
            c.execute("UPDATE submissions SET status='ocr_failed', error=? WHERE id=?",
                      ('Exam has no questions defined yet', sub_id))
        return

    try:
        ocr = ocr_submission(image_paths, questions)
    except Exception as e:
        with db() as c:
            c.execute("UPDATE submissions SET status='ocr_failed', error=? WHERE id=?",
                      (f'OCR failed: {e}', sub_id))
        return

    student_name = str(ocr.get('student_name') or '').strip()
    by_q = {int(a.get('q')): str(a.get('answer') or '') for a in ocr.get('answers', []) if a.get('q') is not None}

    total, maxtot = 0.0, 0.0
    rows = []
    for q in questions:
        sa = by_q.get(q['number'], '')
        if q['type'] == 'mc':
            score, fb = grade_mc(q, sa)
        else:
            score, fb = grade_essay(q, sa)
        total += score
        maxtot += q['max_score']
        rows.append((sub_id, q['number'], sa, score, q['max_score'], fb, 'auto'))

    with db() as c:
        c.executemany('''INSERT INTO answers
            (submission_id, question_number, student_answer, score, max_score, feedback, graded_by)
            VALUES (?,?,?,?,?,?,?)''', rows)
        c.execute('''UPDATE submissions SET student_name=?, status='graded',
                     total_score=?, max_score=?, error='' WHERE id=?''',
                  (student_name, total, maxtot, sub_id))


# ─── Routes: exams ───
@app.route('/')
def index():
    with db() as c:
        exams = c.execute('''SELECT e.*,
            (SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) n_questions,
            (SELECT COUNT(*) FROM submissions s WHERE s.exam_id=e.id) n_submissions
            FROM exams e ORDER BY e.id DESC''').fetchall()
    return render_template('index.html', exams=exams, prefix=PREFIX)


@app.route('/exam/new', methods=['GET', 'POST'])
def exam_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip() or 'Untitled Exam'
        with db() as c:
            cur = c.execute('INSERT INTO exams (name, created_at) VALUES (?,?)', (name, now()))
            eid = cur.lastrowid
        return redirect(f'{PREFIX}/exam/{eid}')
    return render_template('exam_new.html', prefix=PREFIX)


@app.route('/exam/<int:eid>')
def exam_detail(eid):
    with db() as c:
        exam = c.execute('SELECT * FROM exams WHERE id=?', (eid,)).fetchone()
        if not exam:
            return redirect(f'{PREFIX}/')
        questions = c.execute('SELECT * FROM questions WHERE exam_id=? ORDER BY number', (eid,)).fetchall()
        submissions = c.execute('SELECT * FROM submissions WHERE exam_id=? ORDER BY id DESC', (eid,)).fetchall()
    return render_template('exam_detail.html', exam=exam, questions=questions,
                            submissions=submissions, prefix=PREFIX)


@app.route('/exam/<int:eid>/question/add', methods=['POST'])
def question_add(eid):
    f = request.form
    qtype = f.get('type', 'mc')
    with db() as c:
        nxt = c.execute('SELECT COALESCE(MAX(number),0)+1 FROM questions WHERE exam_id=?', (eid,)).fetchone()[0]
        c.execute('''INSERT INTO questions
            (exam_id, number, type, prompt, choices, correct_answer, model_answer, max_score)
            VALUES (?,?,?,?,?,?,?,?)''',
            (eid, nxt, qtype, f.get('prompt', ''), f.get('choices', ''),
             f.get('correct_answer', '').strip(), f.get('model_answer', ''),
             float(f.get('max_score') or 1)))
    return redirect(f'{PREFIX}/exam/{eid}')


@app.route('/exam/<int:eid>/question/<int:qid>/delete', methods=['POST'])
def question_delete(eid, qid):
    with db() as c:
        c.execute('DELETE FROM questions WHERE id=? AND exam_id=?', (qid, eid))
    return redirect(f'{PREFIX}/exam/{eid}')


# ─── Routes: submissions (batch upload = "run multiple") ───
@app.route('/exam/<int:eid>/upload', methods=['POST'])
def upload(eid):
    files = request.files.getlist('files')
    results = []
    for fobj in files:
        if not fobj or not fobj.filename:
            continue
        results.append(_handle_one_file(eid, fobj))
    return jsonify({'ok': True, 'results': results})


def _handle_one_file(eid, fobj):
    orig_name = fobj.filename
    ext = os.path.splitext(orig_name)[1].lower()
    sub_dir = os.path.join(UPLOAD_DIR, str(eid), uuid.uuid4().hex[:12])
    os.makedirs(sub_dir, exist_ok=True)
    raw_path = os.path.join(sub_dir, 'original' + ext)
    fobj.save(raw_path)

    try:
        if ext == '.pdf':
            image_paths = pdf_to_images(raw_path, sub_dir)
        elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
            image_paths = [raw_path]
        else:
            return {'filename': orig_name, 'ok': False, 'error': f'unsupported file type {ext}'}
        if not image_paths:
            return {'filename': orig_name, 'ok': False, 'error': 'no pages/images extracted'}
    except Exception as e:
        return {'filename': orig_name, 'ok': False, 'error': f'could not read file: {e}'}

    rel_paths = [os.path.relpath(p, UPLOAD_DIR) for p in image_paths]
    with db() as c:
        cur = c.execute('''INSERT INTO submissions (exam_id, filename, image_paths, created_at)
                           VALUES (?,?,?,?)''', (eid, orig_name, json.dumps(rel_paths), now()))
        sub_id = cur.lastrowid

    process_submission(sub_id, eid, image_paths)

    with db() as c:
        row = c.execute('SELECT * FROM submissions WHERE id=?', (sub_id,)).fetchone()
    return {'filename': orig_name, 'ok': row['status'] == 'graded', 'id': sub_id,
            'status': row['status'], 'error': row['error'],
            'student_name': row['student_name'],
            'total_score': row['total_score'], 'max_score': row['max_score']}


@app.route('/exam/<int:eid>/submission/<int:sid>')
def submission_detail(eid, sid):
    with db() as c:
        exam = c.execute('SELECT * FROM exams WHERE id=?', (eid,)).fetchone()
        sub = c.execute('SELECT * FROM submissions WHERE id=? AND exam_id=?', (sid, eid)).fetchone()
        answers = c.execute('''SELECT a.*, q.type, q.prompt, q.correct_answer, q.model_answer
            FROM answers a JOIN questions q ON q.exam_id=? AND q.number=a.question_number
            WHERE a.submission_id=? ORDER BY a.question_number''', (eid, sid)).fetchall()
    images = json.loads(sub['image_paths']) if sub and sub['image_paths'] else []
    return render_template('submission_detail.html', exam=exam, sub=sub, answers=answers,
                            images=images, prefix=PREFIX)


@app.route('/exam/<int:eid>/submission/<int:sid>/delete', methods=['POST'])
def submission_delete(eid, sid):
    with db() as c:
        c.execute('DELETE FROM answers WHERE submission_id=?', (sid,))
        c.execute('DELETE FROM submissions WHERE id=? AND exam_id=?', (sid, eid))
    return redirect(f'{PREFIX}/exam/{eid}')


@app.route('/exam/<int:eid>/submission/<int:sid>/override', methods=['POST'])
def answer_override(eid, sid):
    """Teacher correction: override OCR text and/or score for one answer."""
    f = request.form
    qnum = int(f['question_number'])
    new_answer = f.get('student_answer')
    new_score = f.get('score')
    with db() as c:
        if new_answer is not None:
            c.execute('UPDATE answers SET student_answer=? WHERE submission_id=? AND question_number=?',
                      (new_answer, sid, qnum))
        if new_score is not None and new_score != '':
            c.execute('''UPDATE answers SET score=?, graded_by='manual'
                        WHERE submission_id=? AND question_number=?''',
                      (float(new_score), sid, qnum))
        total, maxtot = c.execute(
            'SELECT COALESCE(SUM(score),0), COALESCE(SUM(max_score),0) FROM answers WHERE submission_id=?',
            (sid,)).fetchone()
        c.execute('UPDATE submissions SET total_score=?, max_score=? WHERE id=?', (total, maxtot, sid))
    return redirect(f'{PREFIX}/exam/{eid}/submission/{sid}')


@app.route('/exam/<int:eid>/export.csv')
def export_csv(eid):
    import csv, io
    with db() as c:
        subs = c.execute('SELECT * FROM submissions WHERE exam_id=? ORDER BY student_name', (eid,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['student_name', 'filename', 'status', 'total_score', 'max_score', 'percent'])
    for s in subs:
        pct = round(100 * s['total_score'] / s['max_score'], 1) if s['max_score'] else 0
        w.writerow([s['student_name'], s['filename'], s['status'], s['total_score'], s['max_score'], pct])
    from flask import Response
    return Response(buf.getvalue(), mimetype='text/csv',
                     headers={'Content-Disposition': f'attachment; filename=exam_{eid}_scores.csv'})


@app.route('/uploads/<path:relpath>')
def serve_upload(relpath):
    return send_from_directory(UPLOAD_DIR, relpath)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8090)))
