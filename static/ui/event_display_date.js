export function formatEventDisplayDate(value, timeZone) {
    if (!timeZone || Number.isNaN(value.getTime()))
        return null;
    const lang = document.documentElement.lang || 'en';
    try {
        const text = new Intl.DateTimeFormat(lang, {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone,
        }).format(value);
        return text || null;
    }
    catch {
        return null;
    }
}
