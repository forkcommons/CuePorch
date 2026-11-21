import { privateSettings, sharingFields } from "./library_privacy.js";
export function folderChoices(select, folders, excluded) {
    select.replaceChildren(new Option("Library", ""));
    function append(parent, prefix) {
        for (const folder of folders) {
            if (folder.parent !== parent || folder.id === excluded)
                continue;
            const label = prefix + folder.name;
            select.append(new Option(label, folder.id));
            append(folder.id, label + " / ");
        }
    }
    append(null, "");
}
export async function readUploadPrivacy(folder, load) {
    if (folder) {
        const { settings, has_password, allowed } = await (await load(`/api/library/folders/${encodeURIComponent(folder)}/privacy`)).json();
        if (settings)
            return { ...privateSettings, ...settings, has_password, allowed, folder_override: true };
    }
    return { ...privateSettings, ...await (await load("/api/library/upload-defaults")).json() };
}
export function uploadLibraryFile(file, id, folder, credential, options, progress, endpoint = "/api/library/uploads") {
    const data = new FormData();
    data.set("file", file);
    data.set("upload_id", id);
    data.set("folder", folder);
    if (options)
        data.set("privacy", JSON.stringify(options));
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.upload.onprogress = (event) => {
            if (event.lengthComputable)
                progress(100 * event.loaded / event.total);
        };
        request.upload.onload = () => progress(null);
        request.onload = () => {
            if (request.status >= 200 && request.status < 300) {
                try {
                    resolve(JSON.parse(request.responseText));
                }
                catch (_error) {
                    reject(new Error("The upload response was interrupted. Retry this file."));
                }
                return;
            }
            const page = new DOMParser().parseFromString(request.responseText, "text/html");
            const message = page.querySelector("p")?.textContent?.trim();
            reject(new Error(message || `Upload failed (${request.status}).`));
        };
        request.onerror = () => reject(new Error("Connection lost. Retry this file."));
        request.open("POST", endpoint);
        request.setRequestHeader("Authorization", "Bearer " + credential);
        request.send(data);
    });
}
export function libraryUploader(context) {
    const dialog = document.querySelector("#uploadDialog");
    const destination = document.querySelector("#uploadFolder");
    const picker = document.querySelector("#libraryFiles");
    const list = document.querySelector("#uploadFiles");
    const status = document.querySelector("#uploadStatus");
    const send = document.querySelector("#startUpload");
    const close = document.querySelector("#closeUpload");
    const open = document.querySelector("#uploadLibrary");
    const summary = document.querySelector("#uploadVisibility");
    const overrides = document.querySelector("#uploadPrivacy");
    const fields = sharingFields(document.querySelector("#uploadSharingFields"));
    const entries = [];
    let running = false;
    let defaults = null;
    let opening = 0;
    fields.save.hidden = true;
    fields.form.onsubmit = (event) => event.preventDefault();
    function updateSend() {
        send.disabled = running || !defaults || entries.every((entry) => entry.saved);
    }
    async function readDestination() {
        const invocation = ++opening;
        defaults = null;
        const folder = destination.value;
        summary.textContent = "Reading upload privacy…";
        overrides.open = false;
        overrides.hidden = true;
        updateSend();
        try {
            const settings = await context.defaults(folder);
            if (invocation !== opening)
                return;
            defaults = settings;
            fields.populate(settings);
            overrides.hidden = Boolean(settings.folder_override);
            const label = settings.folder_override ? "Folder privacy" : "Video default";
            summary.textContent = `${label}: ${settings.visibility.replace("_", " ")}. Other files are private.`;
        }
        catch {
            if (invocation !== opening)
                return;
            fields.populate(privateSettings);
            if (folder) {
                summary.textContent = "This folder’s privacy settings could not load. Reopen Upload or choose another folder.";
            }
            else {
                defaults = privateSettings;
                overrides.hidden = false;
                summary.textContent = "Your defaults could not load. Files will be added privately unless you choose another setting.";
            }
        }
        finally {
            updateSend();
        }
    }
    function openUpload() {
        context.folderOptions(destination);
        destination.value = context.folder() || "";
        dialog.showModal();
        void readDestination();
    }
    function enqueue(files) {
        for (const file of files) {
            const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
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
    function upload(entry, folder, options) {
        entry.status.textContent = "Uploading…";
        entry.progress.value = 0;
        return uploadLibraryFile(entry.file, entry.id, folder, context.credential(), options, (progress) => {
            if (progress === null) {
                entry.progress.removeAttribute("value");
                entry.status.textContent = "Preparing for the library…";
            }
            else {
                entry.progress.value = progress;
            }
        });
    }
    open.onclick = openUpload;
    destination.onchange = () => { void readDestination(); };
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
        if (running)
            event.preventDefault();
        else
            clearQueue();
    };
    picker.onchange = () => {
        if (picker.files)
            enqueue(picker.files);
        picker.value = "";
    };
    window.addEventListener("dragover", (event) => {
        if (event.dataTransfer?.types.includes("Files"))
            event.preventDefault();
    });
    window.addEventListener("drop", (event) => {
        const files = event.dataTransfer?.files;
        if (!files?.length)
            return;
        event.preventDefault();
        if (!context.available() || running)
            return;
        if (!dialog.open) {
            if (document.querySelector("dialog[open]"))
                return;
            openUpload();
        }
        enqueue(files);
    });
    send.onclick = async () => {
        if (!defaults)
            return;
        if (overrides.open && !fields.form.reportValidity())
            return;
        const settings = defaults.folder_override ? null
            : overrides.open ? fields.values() : { ...defaults, password: "" };
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
                }
                catch (error) {
                    entry.progress.value = 0;
                    entry.status.textContent = error instanceof Error ? error.message : String(error);
                }
            }
            if (added)
                await context.reload();
            const remaining = entries.filter((entry) => !entry.saved).length;
            status.textContent = remaining ? `${remaining} file(s) need attention.` : "All files added.";
            send.textContent = remaining ? "Retry failed files" : "Upload selected files";
        }
        catch (error) {
            status.textContent = `Files were saved. Reload the library to view them. ${error}`;
        }
        finally {
            running = false;
            close.disabled = picker.disabled = destination.disabled = false;
            updateSend();
        }
    };
}
