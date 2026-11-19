import { privateSettings, sharingFields, type Sharing } from "./library_privacy.js";

interface UploadContext {
  available(): boolean;
  folder(): string | null;
  folderOptions(select: HTMLSelectElement): void;
  credential(): string;
  defaults(): Promise<Sharing>;
  reload(): Promise<void>;
}

interface UploadEntry {
  file: File;
  id: string;
  progress: HTMLProgressElement;
  status: HTMLSpanElement;
  saved: boolean;
}

interface UploadReceipt {
  pending: boolean;
  visibility: Sharing["visibility"];
}

export function libraryUploader(context: UploadContext) {
  const dialog = document.querySelector<HTMLDialogElement>("#uploadDialog")!;
  const destination = document.querySelector<HTMLSelectElement>("#uploadFolder")!;
  const picker = document.querySelector<HTMLInputElement>("#libraryFiles")!;
  const list = document.querySelector<HTMLUListElement>("#uploadFiles")!;
  const status = document.querySelector<HTMLElement>("#uploadStatus")!;
  const send = document.querySelector<HTMLButtonElement>("#startUpload")!;
  const close = document.querySelector<HTMLButtonElement>("#closeUpload")!;
  const open = document.querySelector<HTMLButtonElement>("#uploadLibrary")!;
  const summary = document.querySelector<HTMLElement>("#uploadVisibility")!;
  const overrides = document.querySelector<HTMLDetailsElement>("#uploadPrivacy")!;
  const fields = sharingFields(document.querySelector<HTMLElement>("#uploadSharingFields")!);
  const entries: UploadEntry[] = [];
  let running = false;
  let defaults: Sharing | null = null;
  let opening = 0;
  fields.save.hidden = true;
  fields.form.onsubmit = (event) => event.preventDefault();

  function updateSend() {
    send.disabled = running || !defaults || entries.every((entry) => entry.saved);
  }

  async function openUpload() {
    const invocation = ++opening;
    defaults = null;
    summary.textContent = "Reading your upload defaults…";
    overrides.open = false;
    context.folderOptions(destination);
    destination.value = context.folder() || "";
    dialog.showModal();
    try {
      const settings = await context.defaults();
      if (invocation !== opening) return;
      defaults = settings;
      fields.populate(settings);
      summary.textContent = `Video default: ${settings.visibility.replace("_", " ")}. Other files are private.`;
    } catch {
      if (invocation !== opening) return;
      defaults = privateSettings;
      fields.populate(privateSettings);
      summary.textContent = "Your defaults could not load. Files will be added privately unless you choose another setting.";
    } finally {
      updateSend();
    }
  }

  function enqueue(files: FileList) {
    for (const file of files) {
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join("");
      const row = document.createElement("li");
      const note = document.createElement("span");
      note.textContent = "Ready";
      const progress = document.createElement("progress");
      progress.setAttribute("aria-label", "Upload progress for " + file.name);
      progress.max = 100;
      progress.value = 0;
      row.append(file.name + " · ", note, progress);
      list.append(row);
      entries.push({ file, id, progress, status: note, saved: false });
    }
    updateSend();
  }

  function upload(entry: UploadEntry, folder: string, options: object) {
    const data = new FormData();
    data.set("file", entry.file);
    data.set("upload_id", entry.id);
    data.set("folder", folder);
    data.set("privacy", JSON.stringify(options));
    entry.status.textContent = "Uploading…";
    entry.progress.value = 0;
    return new Promise<UploadReceipt>((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.upload.onprogress = (event) => {
        if (event.lengthComputable) entry.progress.value = 100 * event.loaded / event.total;
      };
      request.upload.onload = () => {
        entry.progress.removeAttribute("value");
        entry.status.textContent = "Preparing for the library…";
      };
      request.onload = () => {
        if (request.status >= 200 && request.status < 300) {
          try {
            resolve(JSON.parse(request.responseText));
          } catch (_error) {
            reject(new Error("The upload response was interrupted. Retry this file."));
          }
          return;
        }
        const page = new DOMParser().parseFromString(request.responseText, "text/html");
        const message = page.querySelector("p")?.textContent?.trim();
        reject(new Error(message || `Upload failed (${request.status}).`));
      };
      request.onerror = () => reject(new Error("Connection lost. Retry this file."));
      request.open("POST", "/api/library/uploads");
      request.setRequestHeader("Authorization", "Bearer " + context.credential());
      request.send(data);
    });
  }

  open.onclick = openUpload;
  function clearQueue() {
    opening++;
    entries.length = 0;
    list.replaceChildren();
    status.textContent = "";
    send.textContent = "Upload selected files";
    defaults = null;
    fields.populate(privateSettings);
    updateSend();
  }
  close.onclick = () => {
    clearQueue();
    dialog.close();
  };
  dialog.oncancel = (event) => {
    if (running) event.preventDefault();
    else clearQueue();
  };
  picker.onchange = () => {
    if (picker.files) enqueue(picker.files);
    picker.value = "";
  };

  window.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
  window.addEventListener("drop", (event) => {
    const files = event.dataTransfer?.files;
    if (!files?.length) return;
    event.preventDefault();
    if (!context.available() || running) return;
    if (!dialog.open) {
      if (document.querySelector("dialog[open]")) return;
      openUpload();
    }
    enqueue(files);
  });

  send.onclick = async () => {
    if (!defaults) return;
    if (overrides.open && !fields.form.reportValidity()) return;
    const settings = overrides.open ? fields.values() : { ...defaults, password: "" };
    const folder = destination.value;
    let added = 0;
    running = true;
    send.disabled = close.disabled = picker.disabled = destination.disabled = true;
    status.textContent = "";
    try {
      for (const entry of entries.filter((item) => !item.saved)) {
        try {
          const receipt = await upload(entry, folder, settings);
          entry.saved = true;
          entry.progress.value = 100;
          entry.status.textContent = receipt.pending ? "Added · sharing awaits approval"
            : receipt.visibility === "private" ? "Added privately" : "Added · " + receipt.visibility.replace("_", " ");
          added++;
        } catch (error) {
          entry.progress.value = 0;
          entry.status.textContent = error instanceof Error ? error.message : String(error);
        }
      }
      if (added) await context.reload();
      const remaining = entries.filter((entry) => !entry.saved).length;
      status.textContent = remaining ? `${remaining} file(s) need attention.` : "All files added.";
      send.textContent = remaining ? "Retry failed files" : "Upload selected files";
    } catch (error) {
      status.textContent = `Files were saved. Reload the library to view them. ${error}`;
    } finally {
      running = false;
      close.disabled = picker.disabled = destination.disabled = false;
      updateSend();
    }
  };
}
