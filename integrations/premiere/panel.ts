declare function require(name: string): unknown;

interface UxpFileMetadata {
  size: number;
}

interface UxpFile {
  getMetadata(): Promise<UxpFileMetadata>;
  read(): Promise<string>;
}

interface UxpLocalFileSystem {
  getFileForOpening(options: { types: string[] }): Promise<UxpFile | null | undefined>;
}

interface UxpModule {
  storage: {
    localFileSystem: UxpLocalFileSystem;
  };
}

const markers = require("./markers.js") as typeof import("./markers");
const applyMod = require("./apply.js") as typeof import("./apply");
const premiere = require("premierepro") as import("./host-types").PremiereHost;
const uxp = require("uxp") as UxpModule;

const MAX_BYTES = 16 * 1024 * 1024;

function setStatus(status: HTMLElement, message: string): void {
  status.textContent = message;
}

async function importMarkers(btn: HTMLButtonElement, status: HTMLElement): Promise<void> {
  btn.disabled = true;
  setStatus(status, "Choose a markers JSON file…");

  try {
    const file = await uxp.storage.localFileSystem.getFileForOpening({ types: ["json"] });
    if (!file) {
      setStatus(status, "Import cancelled.");
      return;
    }

    const { size } = await file.getMetadata();
    if (size > MAX_BYTES) {
      setStatus(status, "File is larger than 16 MB; please pick a smaller markers file.");
      return;
    }

    const text = await file.read();
    const plan: ReturnType<typeof markers.parseReviewMarkers> =
      markers.parseReviewMarkers(text);

    const { added, skipped } = await applyMod.applyReviewMarkers(premiere, plan);
    setStatus(
      status,
      `Imported ${added} marker${added === 1 ? "" : "s"}; skipped ${skipped}.`,
    );
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    setStatus(status, "Import failed: " + reason);
  } finally {
    btn.disabled = false;
  }
}

function bind(): void {
  const btn = document.getElementById("importMarkers") as HTMLButtonElement | null;
  const status = document.getElementById("markerStatus") as HTMLElement | null;
  if (!btn || !status) {
    return;
  }

  btn.addEventListener("click", () => {
    void importMarkers(btn, status);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bind);
} else {
  bind();
}
