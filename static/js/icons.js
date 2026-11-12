// Single-color inline-SVG icon set — same pattern as wqu-quiz.
(function () {
  const S = (d) => `<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-0.15em">${d}</svg>`;
  const ICONS = {
    moon: S('<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>'),
    sun: S('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2 12h2M20 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"/>'),
    plus: S('<path d="M12 5v14M5 12h14"/>'),
    trash: S('<path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14"/>'),
    upload: S('<path d="M12 3v12M7 8l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>'),
    camera: S('<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7l1.5-3h5L16 7"/><circle cx="12" cy="13" r="3.5"/>'),
    file: S('<path d="M7 3h7l3 3v15H7z"/><path d="M14 3v4h4"/>'),
    check: S('<path d="M20 6 9 17l-5-5"/>'),
    x: S('<path d="M18 6 6 18M6 6l12 12"/>'),
    edit: S('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z"/>'),
    'chevron-right': S('<path d="M9 6l6 6-6 6"/>'),
    download: S('<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 19h16"/>'),
    clock: S('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>'),
    alert: S('<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.6 17a1.8 1.8 0 0 0 1.6 2.7h15.6a1.8 1.8 0 0 0 1.6-2.7L13.7 3.9a1.8 1.8 0 0 0-3.4 0z"/>'),
    list: S('<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1" fill="currentColor"/><circle cx="3.5" cy="12" r="1" fill="currentColor"/><circle cx="3.5" cy="18" r="1" fill="currentColor"/>'),
    users: S('<circle cx="9" cy="8" r="3.2"/><path d="M3 20v-1.5A5 5 0 0 1 9 14a5 5 0 0 1 6 4.5V20"/><circle cx="17" cy="9" r="2.6"/><path d="M16.2 14.2A4.3 4.3 0 0 1 21 18v2"/>'),
    'book-open': S('<path d="M12 7c-2-1.5-5-2-8-1v13c3-1 6-.5 8 1 2-1.5 5-2 8-1V6c-3-1-6-.5-8 1z"/><path d="M12 7v14"/>'),
  };
  window.ICON = (name) => ICONS[name] || '';
})();
