export function setupLiveApi(request) {
    const panel = document.querySelector("#liveApiAccess");
    const form = document.querySelector("#liveApiCredential");
    const reveal = document.querySelector("#liveApiReveal");
    const token = reveal.querySelector("input");
    const list = document.querySelector("#liveApiCredentials");
    const status = document.querySelector("#liveApiStatus");
    const path = "/api/operator/live-api/credentials";
    let pending = false;
    function clearToken() {
        token.value = "";
        reveal.hidden = true;
    }
    async function update() {
        const result = await request(path);
        const rows = result.credentials.map(entry => {
            const row = document.createElement("li");
            const expired = entry.expires_at * 1000 <= Date.now();
            const expiry = new Date(entry.expires_at * 1000).toLocaleString();
            const availability = entry.revoked_at ? "Revoked" : expired ? "Expired" : "Expires";
            row.textContent = `${entry.name} (${entry.scopes.join(", ")}). ${availability}${entry.revoked_at ? "" : " " + expiry}. `;
            if (!entry.revoked_at && !expired) {
                const revoke = document.createElement("button");
                revoke.type = "button";
                revoke.textContent = "Revoke";
                revoke.setAttribute("aria-label", "Revoke " + entry.name);
                revoke.onclick = () => void perform(async () => {
                    await request(path + "/" + entry.id, "DELETE");
                    clearToken();
                    await update();
                }, "Token revoked.");
                row.append(revoke);
            }
            return row;
        });
        list.replaceChildren(...rows);
        form.hidden = false;
    }
    async function perform(action, message = "") {
        if (pending)
            return;
        pending = true;
        const buttons = panel.querySelectorAll("button");
        buttons.forEach(button => { button.disabled = true; });
        status.textContent = "";
        try {
            await action();
            status.textContent = message;
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : "The event API request failed.";
        }
        finally {
            buttons.forEach(button => { button.disabled = false; });
            pending = false;
        }
    }
    panel.ontoggle = () => {
        if (panel.open)
            void perform(update);
        else
            clearToken();
    };
    form.onsubmit = (event) => {
        event.preventDefault();
        const fields = new FormData(form);
        void perform(async () => {
            const created = await request(path, "POST", {
                name: fields.get("name"),
                expires_in_days: Number(fields.get("days")),
                scopes: fields.getAll("scope"),
            });
            if (panel.open) {
                token.value = created.token;
                reveal.hidden = false;
                token.focus();
                token.select();
            }
            form.reset();
            await update();
        }, "Token created.");
    };
}
