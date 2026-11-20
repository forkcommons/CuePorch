import { viewerRequest } from "./audience_api.js";
const status = document.querySelector("#viewerCollectionStatus");
let busy = false;
async function update(run) {
    if (busy)
        return;
    busy = true;
    status.textContent = "";
    try {
        await run();
    }
    catch {
        status.textContent = "Could not update the collection. Try again.";
    }
    finally {
        busy = false;
    }
}
for (const form of document.querySelectorAll("[data-viewer-collection-form]")) {
    form.onsubmit = event => {
        event.preventDefault();
        const id = form.dataset.id;
        const endpoint = id ? "collections/" + id : "collections";
        void update(async () => {
            const response = await viewerRequest(endpoint, id ? "PUT" : "POST", Object.fromEntries(new FormData(form)));
            if (response.status === 401) {
                location.assign("/account");
                return;
            }
            if (!response.ok)
                throw new Error();
            const entry = await response.json();
            if (!id) {
                location.assign(entry.url);
                return;
            }
            document.querySelector("#viewerCollectionTitle").textContent = entry.title;
            document.title = entry.title + " · CuePorch";
            const description = document.querySelector("#viewerCollectionDescription");
            description.textContent = entry.description;
            description.hidden = !entry.description;
            status.textContent = "Collection saved.";
        });
    };
}
for (const button of document.querySelectorAll("[data-delete-collection]")) {
    button.onclick = () => {
        void update(async () => {
            const response = await viewerRequest("collections/" + button.dataset.deleteCollection, "DELETE");
            if (response.status === 401) {
                location.assign("/account");
                return;
            }
            if (!response.ok)
                throw new Error();
            location.assign("/watch/collections");
        });
    };
}
