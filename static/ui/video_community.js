import { viewerRequest, viewerVideo } from "./audience_api.js";
export function setupVideoLikes(toolbar, showCollection) {
    const video = viewerVideo(toolbar);
    const endpoint = "likes/" + video.path;
    const button = toolbar.querySelector("[data-like-toggle]");
    const count = toolbar.querySelector("[data-like-count]");
    const notice = toolbar.querySelector("[data-like-status]");
    let liked = false;
    function likes(state) {
        liked = state.liked;
        button.setAttribute("aria-pressed", String(liked));
        count.textContent = `${state.likes} ${state.likes === 1 ? "like" : "likes"}`;
        count.hidden = button.hidden = false;
        showCollection?.();
    }
    void viewerRequest(endpoint).then(async (response) => {
        if (response.ok)
            likes(await response.json());
    }).catch(() => { });
    button.onclick = async () => {
        button.disabled = true;
        notice.textContent = "";
        try {
            const response = await viewerRequest(endpoint, liked ? "DELETE" : "PUT");
            if (response.status === 401) {
                if (toolbar.isConnected)
                    location.assign(video.account);
            }
            else if (response.ok) {
                likes(await response.json());
                notice.textContent = liked ? "Liked." : "Like removed.";
            }
            else
                notice.textContent = "The like could not be updated.";
        }
        catch {
            notice.textContent = "Could not contact CuePorch. Try again.";
        }
        finally {
            button.disabled = false;
        }
    };
}
for (const toolbar of document.querySelectorAll("[data-save-video]")) {
    const video = viewerVideo(toolbar);
    const notice = toolbar.querySelector("[data-like-status]");
    const open = toolbar.querySelector("[data-collect-open]");
    setupVideoLikes(toolbar, () => { open.hidden = false; });
    const dialog = toolbar.querySelector("[data-collect-dialog]");
    const choices = dialog.querySelector("[data-collect-options]");
    const change = dialog.querySelector("[data-collect-change]");
    const form = dialog.querySelector("[data-collect-create]");
    const create = form.querySelector("button");
    const status = dialog.querySelector("[data-collect-status]");
    const path = "collections?" + new URLSearchParams({ kind: video.kind, slug: video.slug });
    let entries = [];
    let busy = false;
    function render(selected = choices.value) {
        choices.replaceChildren(...entries.map(item => new Option(item.title, item.id)));
        const current = entries.find(item => item.id === selected) ?? entries[0];
        choices.value = current?.id ?? "";
        choices.disabled = !current || busy;
        change.disabled = !current || busy;
        create.disabled = busy;
        change.textContent = current?.included ? "Remove video" : "Add video";
    }
    choices.onchange = () => render();
    open.onclick = async () => {
        open.disabled = true;
        notice.textContent = "";
        try {
            const response = await viewerRequest(path);
            if (response.status === 401) {
                location.assign(video.account);
                return;
            }
            if (!response.ok)
                throw new Error();
            entries = await response.json();
            render();
            status.textContent = entries.length ? "" : "Create a collection to add this video.";
            dialog.showModal();
        }
        catch {
            notice.textContent = "Could not open your collections.";
        }
        finally {
            open.disabled = false;
        }
    };
    async function update(run) {
        if (busy)
            return;
        busy = true;
        status.textContent = "";
        render();
        try {
            await run();
        }
        catch {
            status.textContent = "Could not update the collection. Try again.";
        }
        finally {
            busy = false;
            render();
        }
    }
    change.onclick = () => {
        const item = entries.find(entry => entry.id === choices.value);
        if (!item)
            return;
        void update(async () => {
            const response = await viewerRequest(`collections/${item.id}/videos/${video.path}`, item.included ? "DELETE" : "PUT");
            if (response.status === 401) {
                location.assign(video.account);
                return;
            }
            if (response.status === 403 && !item.included) {
                status.textContent = "The publisher has disabled adding this video to collections.";
                return;
            }
            if (!response.ok)
                throw new Error();
            item.included = (await response.json()).included;
            status.textContent = item.included ? "Video added to collection." : "Video removed from collection.";
        });
    };
    form.onsubmit = event => {
        event.preventDefault();
        void update(async () => {
            const response = await viewerRequest("collections", "POST", Object.fromEntries(new FormData(form)));
            if (response.status === 401) {
                location.assign(video.account);
                return;
            }
            if (!response.ok)
                throw new Error();
            const item = await response.json();
            entries.unshift({ id: item.id, title: item.title, included: false });
            form.reset();
            render(item.id);
            status.textContent = "Collection created.";
        });
    };
}
