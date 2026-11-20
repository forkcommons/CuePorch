export function browserIdentity() {
  const key = "cueporchAnalyticsViewer";
  try {
    const saved = localStorage.getItem(key);
    if (saved && /^[a-f0-9]{32}$/.test(saved)) return saved;
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const id = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    localStorage.setItem(key, id);
    return id;
  } catch {
    return undefined;
  }
}
