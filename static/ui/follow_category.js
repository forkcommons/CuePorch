import { viewerRequest } from "./audience_api.js";
for (const widget of document.querySelectorAll("[data-follow-category]")) {
    const button = widget.querySelector("button");
    const signIn = widget.querySelector("a");
    const status = widget.querySelector("[role=status]");
    const name = widget.dataset.followCategory || "";
    const isPresetRemove = widget.dataset.feedRemove === "true";
    async function update(method) {
        status.textContent = "";
        button.disabled = true;
        try {
            let response;
            if (method === "GET") {
                const params = new URLSearchParams({ name });
                response = await viewerRequest(`feed/category?${params.toString()}`, "GET");
            }
            else {
                response = await viewerRequest("feed/category", method, { name });
            }
            if (response.status === 401) {
                button.hidden = true;
                signIn.hidden = false;
                return;
            }
            if (!response.ok) {
                if (method !== "GET")
                    status.textContent = "Could not update your follows.";
                return;
            }
            if (isPresetRemove) {
                if (method === "DELETE") {
                    window.location.reload();
                }
                return;
            }
            const state = await response.json();
            button.setAttribute("aria-pressed", String(state.following));
            button.textContent = state.following ? "Unfollow category" : "Follow category";
            button.hidden = false;
            signIn.hidden = true;
        }
        catch {
            status.textContent = "Could not load your follow status.";
        }
        finally {
            button.disabled = false;
        }
    }
    if (isPresetRemove) {
        button.hidden = false;
        signIn.hidden = true;
        button.setAttribute("aria-pressed", "true");
        button.onclick = () => void update("DELETE");
    }
    else {
        button.hidden = true;
        signIn.hidden = true;
        button.onclick = () => void update(button.getAttribute("aria-pressed") === "true" ? "DELETE" : "PUT");
        void update("GET");
    }
}
for (const button of document.querySelectorAll("[data-feed-unfollow-creator]")) {
    const handle = button.dataset.feedUnfollowCreator || "";
    const status = button.parentElement?.querySelector("[role=status]") ?? null;
    button.onclick = async () => {
        if (status)
            status.textContent = "";
        button.disabled = true;
        try {
            const response = await viewerRequest("follow/" + encodeURIComponent(handle), "DELETE");
            if (!response.ok) {
                if (status)
                    status.textContent = "Could not unfollow creator.";
                return;
            }
            window.location.reload();
        }
        catch {
            if (status)
                status.textContent = "Could not unfollow creator.";
        }
        finally {
            button.disabled = false;
        }
    };
}
