// Loaded deferred on the WooCommerce classic product editor only.
(() => {
  const btn = document.getElementById(
    "cueporchChooseVideo",
  ) as HTMLButtonElement | null;
  const ta = document.getElementById(
    "_cueporch_video_urls",
  ) as HTMLTextAreaElement | null;
  const status = document.getElementById("cueporchPickerStatus");
  if (!btn || !ta || !status) return;

  const raw = btn.dataset.origin ?? "";
  let trustedOrigin: string;
  try {
    const u = new URL(raw);
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
    if (u.protocol !== "https:" && !(u.protocol === "http:" && loopback))
      throw 0;
    trustedOrigin = u.origin;
  } catch {
    status.textContent = "CuePorch picker unavailable: invalid trusted origin.";
    btn.disabled = true;
    return;
  }

  const uuid = (): string => {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };

  let popup: Window | null = null;
  let active: string | null = null;
  const setStatus = (m: string): void => {
    status.textContent = m;
  };

  const append = (url: string): boolean => {
    const lines = ta.value
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (lines.includes(url) || lines.length >= 10) return false;
    lines.push(url);
    ta.value = lines.join("\n");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  };

  window.addEventListener("message", (ev: MessageEvent) => {
    if (!active || !popup || ev.source !== popup) return;
    if (ev.origin !== trustedOrigin) return;
    const d: unknown = ev.data;
    if (!d || typeof d !== "object") return;
    const m = d as Record<string, unknown>;
    if (m.type !== "cueporch-wordpress-selected" || m.request !== active)
      return;
    if (typeof m.embedUrl !== "string") return;
    let eu: URL;
    try {
      eu = new URL(m.embedUrl);
    } catch {
      return;
    }
    if (eu.origin !== trustedOrigin || eu.username || eu.password || eu.hash)
      return;
    if (
      !/^\/embed\/work\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
        eu.pathname,
      ) ||
      !(eu.search === "" || /^\?key=[A-Za-z0-9_-]{1,128}$/.test(eu.search))
    )
      return;
    const embedUrl = eu.href;
    const ok = append(embedUrl);
    setStatus(
      ok ? "Video added." : "Video rejected: duplicate or 10-item limit.",
    );
    active = null;
  });

  btn.addEventListener("click", () => {
    try {
      if (popup && !popup.closed) popup.close();
    } catch {
      /* ignore */
    }
    active = uuid();
    const qs = new URLSearchParams({
      destination: location.origin,
      request: active,
    });
    const target = `${trustedOrigin}/operator/wordpress-media-picker?${qs.toString()}`;
    let w: Window | null = null;
    try {
      w = window.open(target, "cueporchPicker", "width=960,height=720");
    } catch {
      /* ignore */
    }
    if (!w) {
      active = null;
      popup = null;
      setStatus("Picker blocked or unavailable. Allow popups and retry.");
      return;
    }
    popup = w;
    setStatus("Picker opened. Select a video to continue.");
  });
})();
