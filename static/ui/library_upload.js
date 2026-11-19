export function libraryUploader(context) {
    const dialog = document.querySelector("#uploadDialog");
    const destination = document.querySelector("#uploadFolder");
    const picker = document.querySelector("#libraryFiles");
    const list = document.querySelector("#uploadFiles");
    const status = document.querySelector("#uploadStatus");
    const send = document.querySelector("#startUpload");
    const close = document.querySelector("#closeUpload");
    const open = document.querySelector("#uploadLibrary");
    const entries = [];
    let running = false;
    function openUpload() {
        context.folderOptions(destination);
        destination.value = context.folder() || "";
        dialog.showModal();
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
        send.disabled = !entries.some((entry) => !entry.saved);
    }
    function upload(entry, folder) {
        const data = new FormData();
        data.set("file", entry.file);
        data.set("upload_id", entry.id);
        data.set("folder", folder);
        entry.status.textContent = "Uploading…";
        entry.progress.value = 0;
        return new Promise((resolve, reject) => {
            const request = new XMLHttpRequest();
            request.upload.onprogress = (event) => {
                if (event.lengthComputable)
                    entry.progress.value = 100 * event.loaded / event.total;
            };
            request.upload.onload = () => {
                entry.progress.removeAttribute("value");
                entry.status.textContent = "Preparing for the library…";
            };
            request.onload = () => {
                if (request.status >= 200 && request.status < 300) {
                    resolve();
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
        entries.length = 0;
        list.replaceChildren();
        status.textContent = "";
        send.textContent = "Upload selected files";
        send.disabled = true;
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
        const folder = destination.value;
        let added = 0;
        running = true;
        send.disabled = close.disabled = picker.disabled = destination.disabled = true;
        status.textContent = "";
        try {
            for (const entry of entries.filter((item) => !item.saved)) {
                try {
                    await upload(entry, folder);
                    entry.saved = true;
                    entry.progress.value = 100;
                    entry.status.textContent = "Added privately";
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
            send.disabled = entries.every((entry) => entry.saved);
        }
    };
}
