export function interpreterControls(request) {
    const panel = document.getElementById("interpreterPanel");
    const rows = document.getElementById("interpreterRows");
    const form = document.getElementById("interpreterForm");
    const languageInput = document.getElementById("interpreterLanguage");
    const nameInput = document.getElementById("interpreterName");
    const refreshInterpreters = document.getElementById("refreshInterpreters");
    const status = document.getElementById("interpreterStatus");
    let currentPath = null;
    let generation = 0;
    let busy = false;
    panel.hidden = true;
    const setInert = (value) => {
        panel.inert = value;
    };
    const clear = () => {
        rows.replaceChildren();
        status.textContent = "";
        form.reset();
    };
    const btn = (text, onClick) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = text;
        b.addEventListener("click", onClick);
        return b;
    };
    function render(items) {
        rows.replaceChildren(...items.map((item) => {
            const li = document.createElement("li");
            const label = document.createElement("span");
            const human = item.state === "receiving" ? "Receiving audio" : "Waiting for encoder";
            label.textContent = `${item.name} — ${human}`;
            const server = document.createElement("input");
            server.type = "text";
            server.readOnly = true;
            server.value = item.server;
            server.setAttribute("aria-label", "Interpreter server");
            const key = document.createElement("input");
            key.type = "password";
            key.readOnly = true;
            key.value = item.key;
            key.setAttribute("aria-label", "Interpreter stream key");
            const copy = btn("Copy key", () => void navigator.clipboard.writeText(item.key));
            const remove = btn("Remove", () => void run(async (p) => {
                await request(p + "/" + encodeURIComponent(item.language), "DELETE");
                return (await request(p, "GET")).inputs;
            }, show));
            li.append(label, server, key, copy, remove);
            return li;
        }));
    }
    const show = (items) => {
        render(items);
        status.textContent = "";
    };
    const run = async (fn, update) => {
        const path = currentPath, gen = generation;
        if (!path || busy)
            return;
        busy = true;
        setInert(true);
        status.textContent = "Loading…";
        try {
            const items = await fn(path);
            if (gen === generation && path === currentPath)
                update(items);
        }
        catch (err) {
            if (gen === generation && path === currentPath)
                status.textContent = err instanceof Error ? err.message : "Request failed";
        }
        finally {
            if (gen === generation && path === currentPath) {
                busy = false;
                setInert(false);
            }
        }
    };
    const load = () => run(async (p) => (await request(p, "GET")).inputs, show);
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        const language = languageInput.value.trim(), name = nameInput.value.trim();
        if (!language || !name)
            return;
        void run(async (p) => {
            await request(p, "POST", { language, name });
            return (await request(p, "GET")).inputs;
        }, (items) => {
            show(items);
            form.reset();
        });
    });
    refreshInterpreters.addEventListener("click", () => void load());
    panel.addEventListener("toggle", () => {
        if (panel.open)
            void load();
    });
    return (path) => {
        if (path === currentPath)
            return;
        generation++;
        currentPath = path;
        busy = false;
        setInert(false);
        clear();
        panel.hidden = !path;
        panel.open = false;
    };
}
