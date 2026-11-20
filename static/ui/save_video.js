import { viewerRequest, viewerVideo } from "./audience_api.js";
for (const toolbar of document.querySelectorAll("[data-save-video]")) {
    const button = toolbar.querySelector("[data-save-toggle]");
    const status = toolbar.querySelector("[data-save-status]");
    const video = viewerVideo(toolbar);
    const endpoint = "watch-later/" + video.path;
    let saved = false;
    function render(value) {
        saved = value;
        button.textContent = saved ? "Remove from Watch Later" : "Save to Watch Later";
        button.hidden = false;
    }
    void viewerRequest(endpoint).then(async (response) => {
        if (response.ok)
            render((await response.json()).saved);
        else if (response.status === 401)
            render(false);
    }).catch(() => { });
    button.onclick = async () => {
        button.disabled = true;
        status.textContent = "";
        try {
            const response = await viewerRequest(endpoint, saved ? "DELETE" : "PUT");
            if (response.status === 401)
                location.assign(video.account);
            else if (response.ok) {
                render((await response.json()).saved);
                status.textContent = saved ? "Saved." : "Removed.";
            }
            else
                status.textContent = "This video could not be saved.";
        }
        catch {
            status.textContent = "Could not contact CuePorch. Try again.";
        }
        finally {
            button.disabled = false;
        }
    };
}
for (const button of document.querySelectorAll("[data-remove-video]")) {
    const status = document.querySelector("#watchLaterStatus");
    const empty = document.querySelector("#watchLaterEmpty");
    button.onclick = async () => {
        button.disabled = true;
        try {
            const response = await viewerRequest(button.dataset.endpoint + "/" + viewerVideo(button).path, "DELETE");
            if (response.status === 401) {
                location.assign("/account");
                return;
            }
            if (!response.ok)
                throw new Error();
            const row = button.closest("[data-saved-row]");
            const neighbor = row.nextElementSibling ?? row.previousElementSibling;
            row.remove();
            empty.hidden = !!document.querySelector("[data-saved-row]");
            status.textContent = "Video removed.";
            status.tabIndex = -1;
            (neighbor?.querySelector("button") ?? status).focus();
        }
        catch {
            status.textContent = "The video could not be removed. Try again.";
            button.disabled = false;
        }
    };
}
