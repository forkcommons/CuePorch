const lang = document.documentElement.lang || 'en';
const sel = 'time[data-event-opens][data-event-timezone]';
for (const el of document.querySelectorAll(sel)) {
    const raw = el.getAttribute('data-event-opens');
    const timeZone = el.getAttribute('data-event-timezone');
    const secs = raw == null ? NaN : Number(raw);
    if (!Number.isFinite(secs) || !timeZone)
        continue;
    const d = new Date(secs * 1000);
    if (Number.isNaN(d.getTime()))
        continue;
    try {
        const text = new Intl.DateTimeFormat(lang, {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone,
        }).format(d);
        if (text) {
            el.dateTime = d.toISOString();
            el.textContent = text;
        }
    }
    catch { /* preserve existing text */ }
}
export {};
