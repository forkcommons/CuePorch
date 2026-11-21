import { checked } from "./team_api.js";
const labels = {
    queued: "Queued",
    waiting: "Waiting",
    sent: "Sent",
    submitted: "Submitted",
    pending_confirmation: "Pending confirmation",
    skipped: "Skipped",
    canceled: "Canceled",
    tracking_stopped: "Tracking stopped",
    problem: "Problem",
    unconfirmed: "Unconfirmed",
};
async function request(credential, path, method = "GET", body) {
    const headers = {};
    const token = credential();
    if (token)
        headers.Authorization = `Bearer ${token}`;
    const mutation = method !== "GET";
    if (mutation) {
        headers["X-CuePorch-Integration"] = "1";
        if (body !== undefined)
            headers["Content-Type"] = "application/json";
    }
    const response = await checked(await fetch(path, {
        method,
        headers,
        body: mutation && body !== undefined ? JSON.stringify(body) : undefined,
    }));
    return response.json();
}
function receiptStatus(receipt, ready, provider) {
    const name = provider === "constant-contact" ? "Constant Contact" : "Campaign Monitor";
    let status;
    if (receipt.state === "stale") {
        status = `Reconnect ${name} and choose a list again.`;
    }
    else if (!receipt.available) {
        status = `Connect ${name} in Integrations.`;
    }
    else if (!ready) {
        status = "Save an enabled registration form with first and last name fields.";
    }
    else if (receipt.state === "connected") {
        status = `${name} delivery is enabled.`;
    }
    else {
        status = `${name} delivery is off.`;
    }
    const counts = Object.entries(labels).flatMap(([key, label]) => receipt.states[key] ? [label + ": " + receipt.states[key]] : []);
    return counts.length ? `${status} ${counts.join(" ")}` : status;
}
export function setupLibraryListDelivery(panel, credential, isCurrent, provider) {
    const prefix = provider === "constant-contact"
        ? "registrationConstantContact"
        : "registrationCampaignMonitor";
    const control = (suffix) => panel.querySelector("#" + prefix + suffix);
    const load = control("Load");
    const more = provider === "constant-contact" ? control("More") : null;
    const client = provider === "campaign-monitor" ? control("Client") : null;
    const picker = control("List");
    const save = control("Save");
    const reload = control("Refresh");
    const stop = control("Stop");
    const status = control("Status");
    let generation = 0;
    return async (endpoint, settings) => {
        const scope = ++generation;
        const active = () => generation === scope && isCurrent(endpoint);
        const ready = settings.enabled && settings.first_name !== "off" && settings.last_name !== "off";
        const lists = new Map();
        let receipt;
        let nextCursor = null;
        let busy = false;
        let savedListVisible = true;
        if (client) {
            client.replaceChildren(new Option("Choose a client", ""));
            client.value = "";
        }
        function drawControls() {
            const disabled = busy || !receipt?.available || !ready;
            load.disabled = disabled;
            if (more) {
                more.hidden = nextCursor === null;
                more.disabled = disabled;
            }
            if (client)
                client.disabled = disabled;
            picker.disabled = disabled || picker.options.length <= 1;
            save.disabled = disabled || !picker.value;
            reload.disabled = busy;
            stop.hidden = !receipt || receipt.state === "off";
            stop.disabled = busy;
        }
        function drawLists(selected) {
            const options = new Map(lists);
            if (savedListVisible && receipt?.list_id && receipt.list_name && !options.has(receipt.list_id)) {
                options.set(receipt.list_id, receipt.list_name);
            }
            picker.replaceChildren(new Option("Choose a list", ""), ...Array.from(options, ([id, name]) => new Option(name, id)));
            picker.value = options.has(selected) ? selected : "";
        }
        async function run(task) {
            if (!active() || busy)
                return;
            busy = true;
            drawControls();
            try {
                await task();
                if (active() && receipt)
                    status.textContent = receiptStatus(receipt, ready, provider);
            }
            catch (error) {
                if (active()) {
                    panel.hidden = false;
                    status.textContent = error instanceof Error ? error.message : String(error);
                }
            }
            finally {
                busy = false;
                if (active())
                    drawControls();
            }
        }
        async function readReceipt(method = "GET", body) {
            const updated = await request(credential, endpoint, method, body);
            if (!active())
                return;
            receipt = updated;
            savedListVisible = true;
            panel.hidden = !receipt.available && receipt.state === "off";
            drawLists(receipt.list_id ?? "");
        }
        async function loadLists(cursor) {
            const query = cursor ? "?" + new URLSearchParams({ cursor }) : "";
            const page = await request(credential, "/api/integrations/constant-contact/lists" + query);
            if (!active())
                return;
            if (!cursor)
                lists.clear();
            for (const list of page.lists)
                lists.set(list.id, list.name);
            nextCursor = page.next_cursor;
            drawLists(picker.value);
        }
        async function loadClients() {
            const data = await request(credential, "/api/integrations/campaign-monitor/clients");
            if (!active() || !client)
                return;
            const previous = client.value;
            client.replaceChildren(new Option("Choose a client", ""), ...data.clients.map(({ id, name }) => new Option(name, id)));
            const keep = data.clients.some(({ id }) => id === previous);
            client.value = keep ? previous : "";
            if (previous && !keep) {
                lists.clear();
                nextCursor = null;
                savedListVisible = false;
                drawLists("");
            }
        }
        async function loadClientLists(clientId) {
            const data = await request(credential, "/api/integrations/campaign-monitor/clients/" + encodeURIComponent(clientId) + "/lists");
            if (!active() || !client || client.value !== clientId)
                return;
            lists.clear();
            for (const list of data.lists)
                lists.set(list.id, list.name);
            nextCursor = null;
            drawLists(picker.value);
        }
        panel.hidden = true;
        status.textContent = "";
        lists.clear();
        nextCursor = null;
        savedListVisible = true;
        drawLists("");
        picker.onchange = drawControls;
        load.onclick = () => {
            if (provider === "constant-contact") {
                void run(() => loadLists());
            }
            else {
                void run(() => loadClients());
            }
        };
        if (more) {
            more.onclick = () => {
                const cursor = nextCursor;
                if (cursor)
                    void run(() => loadLists(cursor));
            };
        }
        if (client) {
            client.onchange = () => {
                const clientId = client.value;
                lists.clear();
                savedListVisible = false;
                nextCursor = null;
                drawLists("");
                if (clientId)
                    void run(() => loadClientLists(clientId));
                else
                    drawControls();
            };
        }
        save.onclick = () => {
            const listId = picker.value;
            if (listId)
                void run(() => readReceipt("PUT", { list_id: listId }));
        };
        reload.onclick = () => void run(() => readReceipt());
        stop.onclick = () => void run(() => readReceipt("DELETE", {}));
        await run(() => readReceipt());
    };
}
