type PickerBinding = {
  id: string;
  popup: Window;
  origin: string;
  sourceButton?: HTMLButtonElement;
  selected: (url: string) => void;
  readingPhotos: boolean;
};
interface Window {
  cueporchPickVideo: (
    origin: string,
    selected: (url: string) => void,
    status: (message: string) => void,
    sourceButton?: HTMLButtonElement,
  ) => void;
}

(() => {
  let active: PickerBinding | null = null;

  const uuid = (): string => {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };

  function trustedPickerEmbed(raw: string, origin: string): string | null {
    let u: URL;
    try {
      u = new URL(raw);
    } catch {
      return null;
    }
    if (
      u.origin !== origin ||
      u.username ||
      u.password ||
      u.hash ||
      u.href.includes("#")
    )
      return null;
    if (
      !/^\/embed\/work\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
        u.pathname,
      )
    )
      return null;
    if (u.search !== "" && !/^\?key=[A-Za-z0-9_-]{1,128}$/.test(u.search))
      return null;
    return u.href;
  }

  function openPicker(
    origin: string,
    selected: (url: string) => void,
    status: (message: string) => void,
    sourceButton?: HTMLButtonElement,
  ): void {
    try {
      if (active) {
        try {
          active.popup.close();
        } catch {}
        active = null;
      }
      const parsed = new URL(origin);
      const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(
        parsed.hostname,
      );
      if (
        parsed.origin !== origin ||
        (parsed.protocol !== "https:" &&
          !(parsed.protocol === "http:" && loopback))
      ) {
        status("Invalid origin");
        return;
      }
      const id = uuid();
      const url = new URL("/operator/wordpress-media-picker", parsed.origin);
      url.searchParams.set("destination", location.origin);
      url.searchParams.set("request", id);
      if (sourceButton) url.searchParams.set("mode", "create");
      const popup = window.open(
        url.toString(),
        "wordpress-media-picker",
        "width=1000,height=700",
      );
      if (!popup) {
        status("Popup blocked");
        return;
      }
      active = {
        id,
        popup,
        origin: parsed.origin,
        sourceButton,
        selected,
        readingPhotos: false,
      };
      status("Picker opened.");
    } catch {
      status("Unable to open picker");
    }
  }

  window.addEventListener("message", (event: MessageEvent) => {
    const binding = active;
    if (!binding) return;
    if (event.origin !== binding.origin) return;
    if (event.source !== binding.popup) return;
    if (typeof event.data !== "object" || event.data === null) return;
    const data = event.data as Record<string, unknown>;
    if (data.request !== binding.id) return;
    if (data.type === "cueporch-wordpress-product-request") {
      if (!binding.sourceButton || binding.readingPhotos) return;
      binding.readingPhotos = true;
      void sendProductSource(
        binding.sourceButton,
        binding.popup,
        binding.origin,
        binding.id,
      ).finally(() => {
        binding.readingPhotos = false;
      });
      return;
    }
    if (data.type === "cueporch-wordpress-selected") {
      if (typeof data.embedUrl !== "string") return;
      const url = trustedPickerEmbed(data.embedUrl, binding.origin);
      if (url === null) return;
      active = null;
      binding.selected(url);
    }
  });

  function bindClassicProductPicker(): void {
    const choose = document.getElementById(
      "cueporchChooseVideo",
    ) as HTMLButtonElement | null;
    const textarea = document.getElementById(
      "_cueporch_video_urls",
    ) as HTMLTextAreaElement | null;
    const status = document.getElementById("cueporchPickerStatus");
    if (!choose || !textarea || !status) return;
    const create = document.getElementById(
      "cueporchCreateProductVideo",
    ) as HTMLButtonElement | null;
    const origin = choose.dataset.origin ?? "";
    const setStatus = (msg: string): void => {
      status.textContent = msg;
    };
    const selected = (url: string): void => {
      const line = url.trim();
      const lines = textarea.value
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean);
      if (lines.includes(line) || lines.length >= 10) {
        setStatus("Video rejected: duplicate or 10-item limit.");
        return;
      }
      lines.push(line);
      textarea.value = lines.join("\n");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      setStatus("Video added.");
    };
    choose.addEventListener("click", () => {
      openPicker(origin, selected, setStatus);
    });
    if (create) {
      create.addEventListener("click", () => {
        openPicker(origin, selected, setStatus, create);
      });
    }
  }

  window.cueporchPickVideo = openPicker;
  bindClassicProductPicker();
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
