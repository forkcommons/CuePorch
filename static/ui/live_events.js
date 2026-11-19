const element = (id) => document.getElementById(id);
const input = (id) => element(id);
const button = (id) => element(id);
const identify = element("eventsIdentify");
const credential = identify.elements.namedItem("credential");
const status = element("eventsStatus");
const workspace = element("eventsWorkspace");
const form = element("newEventForm");
const field = (name) => form.elements.namedItem(name);
const preset = element("eventPreset");
const output = element("eventProduction");
const health = element("eventHealth");
const encoder = element("encoderSettings");
const key = input("encoderKey");
const starting = input("calendarFirst"), ending = input("calendarLast");
const root = "/api/live-events";
const publicBase = document.body.dataset.publicUrl || location.origin;
let calendar;
let active;
let pending = false;
credential.value = sessionStorage.getItem("cueporchOperator") || "";
async function request(path, method = "GET", body) {
    const headers = { Authorization: "Bearer " + credential.value };
    if (body)
        headers["Content-Type"] = "application/json";
    const result = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    if (!result.ok) {
        const detail = new DOMParser().parseFromString(await result.text(), "text/html").querySelector("p")?.textContent;
        throw new Error(detail || `Request failed (${result.status})`);
    }
    return result.status === 204 ? undefined : result.json();
}
async function action(work, message = "") {
    if (pending)
        return;
    pending = true;
    status.textContent = "";
    workspace.inert = true;
    try {
        await work();
        status.textContent = message;
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        pending = false;
        workspace.inert = false;
    }
}
function mediaPath(suffix) {
    return `${root}/${active.event.id}/${active.instance.id}/${suffix}`;
}
function choose(event, instance) {
    active = { event, instance };
    output.hidden = false;
    encoder.hidden = true;
    key.value = "";
    health.textContent = instance.delivery === "ready" ? "Encoder is ready." : instance.delivery.replaceAll("_", " ");
    button("prepareEncoder").disabled = !calendar.host_ready || ["withdrawing", "withdrawn"].includes(instance.delivery);
    button("withdrawEvent").disabled = !calendar.host_ready || ["scheduled", "withdrawn"].includes(instance.delivery);
    element("productionTitle").textContent = `${event.event.title} · ${instance.local.replace("T", " ")} ${event.event.schedule.timezone}`;
    const watch = element("eventWatch");
    element("eventShare").hidden = !event.watch;
    element("eventEmbedField").hidden = !event.embed;
    if (event.watch)
        watch.href = new URL(event.watch + "?occurrence=" + instance.id, publicBase).href;
    const address = event.embed ? new URL(event.embed + "?occurrence=" + instance.id, publicBase).href : "";
    input("eventEmbedCode").value = address ? `<iframe src="${address}" title="Live event" allowfullscreen></iframe>` : "";
}
function readCalendar(value) {
    calendar = value;
    element("eventsHost").hidden = value.host_ready;
    preset.replaceChildren(new Option("Start from scratch", ""), ...value.events.map((entry) => new Option(entry.event.title, entry.id)));
    const rows = element("eventCalendarRows");
    rows.replaceChildren();
    const sorted = value.events.flatMap((event) => event.occurrences.map((instance) => ({ event, instance })))
        .sort((a, b) => a.instance.id - b.instance.id);
    for (const { event, instance } of sorted) {
        const row = rows.insertRow();
        row.insertCell().textContent = event.event.title;
        const timestamp = document.createElement("time");
        timestamp.dateTime = instance.start;
        timestamp.textContent = instance.local.replace("T", " ") + " " + event.event.schedule.timezone;
        row.insertCell().append(timestamp);
        row.insertCell().textContent = event.publication === "waiting" ? "Waiting for publication approval" : instance.delivery.replaceAll("_", " ");
        row.insertCell().textContent = value.overlaps.includes(event.id) ? "Overlapping time" : "";
        const control = document.createElement("button");
        control.type = "button";
        control.textContent = "Open production";
        control.setAttribute("aria-label", `Open production for ${event.event.title} at ${instance.local.replace("T", " ")}`);
        control.onclick = () => choose(event, instance);
        row.insertCell().append(control);
        if (active?.event.id === event.id && active.instance.id === instance.id)
            choose(event, instance);
    }
}
async function load() {
    const query = new URLSearchParams();
    if (starting.value)
        query.set("from", starting.value);
    if (ending.value)
        query.set("to", ending.value);
    readCalendar(await request(root + "?" + query));
    sessionStorage.setItem("cueporchOperator", credential.value);
    identify.hidden = true;
    workspace.hidden = false;
}
identify.onsubmit = (event) => { event.preventDefault(); void action(load); };
if (credential.value)
    void action(load);
button("calendarRefresh").onclick = () => void action(load);
button("newEvent").onclick = () => {
    form.reset();
    field("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    element("recurringFields").hidden = true;
    form.hidden = false;
    field("title").focus();
};
field("repeat").onchange = () => {
    element("recurringFields").hidden = field("repeat").value === "none";
    element("eventWeekdays").hidden = field("repeat").value !== "weeks";
};
preset.onchange = () => {
    const value = calendar.events.find((entry) => entry.id === preset.value)?.event;
    if (!value)
        return;
    for (const name of ["type", "description", "latency"])
        field(name).value = value[name];
    field("save_replay").checked = value.save_replay;
    field("timezone").value = value.schedule.timezone;
};
form.onsubmit = (event) => {
    event.preventDefault();
    void action(async () => {
        const read = new FormData(form);
        const repeat = field("repeat").value;
        const schedule = { timezone: field("timezone").value, start: field("start").value, end: field("end").value,
            repeat, interval: Number(field("interval").value || 1), weekdays: read.getAll("weekday").map(Number),
            until: repeat !== "none" && field("until").value || null, count: repeat !== "none" && Number(field("count").value) || null };
        const created = await request(root, "POST", {
            slug: field("slug").value, title: field("title").value, description: field("description").value,
            type: field("type").value, visibility: field("visibility").value, latency: field("latency").value,
            save_replay: field("save_replay").checked, schedule,
        });
        form.hidden = true;
        await load();
        if (created.occurrences.length)
            choose(created, created.occurrences[0]);
    }, "Event saved.");
};
button("prepareEncoder").onclick = () => void action(async () => {
    const connection = await request(mediaPath("encoder"), "POST");
    input("rtmpServer").value = connection.server;
    input("rtmpsServer").value = connection.secure_server || "";
    element("secureEncoder").hidden = !connection.secure_server;
    key.value = connection.key;
    encoder.hidden = false;
    button("withdrawEvent").disabled = false;
    health.textContent = "Waiting for the encoder.";
});
button("copyEncoderKey").onclick = () => void action(async () => {
    await navigator.clipboard.writeText(key.value);
}, "Stream key copied.");
button("readEventHealth").onclick = () => void action(async () => {
    const result = await request(mediaPath("health"));
    health.textContent = result.state.replaceAll("_", " ") + (typeof result.viewers === "number" ? ` · ${result.viewers} viewers` : "");
});
button("withdrawEvent").onclick = () => void action(async () => {
    encoder.hidden = true;
    key.value = "";
    await request(mediaPath("delivery"), "DELETE");
    await load();
}, "Occurrence withdrawn.");
export {};
