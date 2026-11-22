import { formatEventDisplayDate } from './event_display_date.js';

const sel = 'time[data-event-opens][data-event-timezone]';
for (const el of document.querySelectorAll<HTMLTimeElement>(sel)) {
  const raw = el.getAttribute('data-event-opens');
  const timeZone = el.getAttribute('data-event-timezone');
  const secs = raw == null ? NaN : Number(raw);
  const d = new Date(secs * 1000);
  const text = formatEventDisplayDate(d, timeZone);
  if (text) { el.dateTime = d.toISOString(); el.textContent = text; }
}
