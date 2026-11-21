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

  const createButton = document.getElementById(
    "cueporchCreateProductVideo",
  ) as HTMLButtonElement | null;
  let creationRequested = false;
  let sourceSent = false;
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
    if (m.type === "cueporch-wordpress-product-request") {
      if (
        m.request === active &&
        creationRequested &&
        createButton &&
        !sourceSent
      ) {
        sourceSent = true;
        const request = active;
        void sendProductSource(
          createButton,
          popup,
          trustedOrigin,
          request,
        ).finally(() => {
          if (active === request) sourceSent = false;
        });
      }
      return;
    }
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

  function openPicker(create: boolean): void {
    try {
      if (popup && !popup.closed) popup.close();
    } catch {
      /* ignore */
    }
    active = uuid();
    creationRequested = create;
    sourceSent = false;
    const qs = new URLSearchParams({
      destination: location.origin,
      request: active,
    });
    if (create) qs.set("mode", "create");
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
  }
  btn.addEventListener("click", () => openPicker(false));
  if (createButton)
    createButton.addEventListener("click", () => openPicker(true));
})();

type PhotoCandidate = {
  name: string;
  mime: "image/jpeg" | "image/png" | "image/webp";
  url: string;
};
async function readProductPhoto(source: PhotoCandidate): Promise<{
  name: string;
  mediaType: PhotoCandidate["mime"];
  bytes: ArrayBuffer;
}> {
  const u = new URL(source.url);
  if (u.origin !== location.origin || u.username || u.password || u.hash)
    throw new Error("Invalid photo URL");
  const res = await fetch(source.url, {
    credentials: "same-origin",
    redirect: "error",
    signal: AbortSignal.timeout(12000),
  });
  if (!res.ok) throw new Error("Photo request failed");
  const ct = res.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  if (ct !== source.mime) throw new Error("Invalid photo type");
  const MAX = 8 * 1024 * 1024;
  const cl = res.headers.get("content-length");
  if (cl !== null && /^\d+$/.test(cl) && Number(cl) > MAX)
    throw new Error("Photo too large");
  if (!res.body) throw new Error("Empty photo");
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX) {
      await reader.cancel();
      throw new Error("Photo too large");
    }
    chunks.push(value);
  }
  if (total === 0) throw new Error("Empty photo");
  const buf = new ArrayBuffer(total);
  const bytes = new Uint8Array(buf);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { name: source.name, mediaType: source.mime, bytes: buf };
}

async function sendProductSource(
  button: HTMLButtonElement,
  target: Window,
  origin: string,
  request: string,
): Promise<void> {
  try {
    const title = (button.dataset.title ?? "").trim();
    const candidates: unknown = JSON.parse(button.dataset.photos ?? "");
    if (
      !title ||
      !Array.isArray(candidates) ||
      candidates.length < 1 ||
      candidates.length > 8
    )
      throw new Error();
    const photos: Awaited<ReturnType<typeof readProductPhoto>>[] = [];
    for (const candidate of candidates) {
      const photo = candidate as {
        name?: unknown;
        url?: unknown;
        mime?: unknown;
      };
      if (
        typeof photo.name !== "string" ||
        typeof photo.url !== "string" ||
        (photo.mime !== "image/jpeg" &&
          photo.mime !== "image/png" &&
          photo.mime !== "image/webp")
      )
        throw new Error();
      photos.push(
        await readProductPhoto({
          name: photo.name,
          url: photo.url,
          mime: photo.mime as PhotoCandidate["mime"],
        }),
      );
    }
    target.postMessage(
      { type: "cueporch-wordpress-product-source", request, title, photos },
      origin,
      photos.map((photo) => photo.bytes),
    );
  } catch {
    try {
      target.postMessage(
        { type: "cueporch-wordpress-product-error", request },
        origin,
      );
    } catch {}
  }
}
