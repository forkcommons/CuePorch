const identify = document.querySelector("#identify");
const key = document.querySelector("#operatorKey");
const status = document.querySelector("#detailsStatus");
const pane = document.querySelector("#videoDetails");
const form = document.querySelector("#videoMeta");
const submit = form.querySelector("button[type=submit]");
const facts = document.querySelector("#videoFacts");
const heading = document.querySelector("#videoHeading");
const descriptors = document.querySelector("#matureReasons");
const input = (name) => form.elements.namedItem(name);
const rating = form.elements.namedItem("rating");
key.value = sessionStorage.getItem("cueporchOperator") || "";
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "") + "/details";
async function send(method = "GET", body) {
    const response = await fetch(endpoint, {
        method,
        headers: { Authorization: "Bearer " + key.value, "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        throw new Error(html.querySelector("p")?.textContent || `Request failed (${response.status})`);
    }
    return response.json();
}
function contentVisibility() {
    descriptors.hidden = descriptors.disabled = rating.value !== "mature";
}
rating.onchange = contentVisibility;
function show(details) {
    heading.textContent = details.title;
    for (const name of ["title", "description", "license", "rating"]) {
        input(name).value = details[name] || "";
    }
    for (const name of ["tags", "categories"])
        input(name).value = details[name].join("\n");
    input("credits").value = details.credits.map(({ name, role }) => role ? `${name} | ${role}` : name).join("\n");
    input("realistic_ai").checked = details.realistic_ai;
    input("advertisement").checked = details.advertisement;
    for (const control of form.querySelectorAll("[name=content]")) {
        control.checked = details.content.includes(control.value);
    }
    contentVisibility();
    const media = details.media;
    const values = {
        Format: media?.format.toUpperCase(),
        Resolution: media?.width && media.height ? `${media.width} × ${media.height}` : "",
        "File size": media ? `${media.bytes.toLocaleString()} bytes` : "",
        "Frame rate": media?.fps ? `${media.fps} fps` : "",
        Audio: media?.audio,
        Projection: details.projection,
        Duration: details.duration_seconds === null ? "" : `${details.duration_seconds} seconds`,
        Added: details.added_at ? new Date(details.added_at).toLocaleDateString() : "",
    };
    facts.replaceChildren();
    for (const [label, value] of Object.entries(values)) {
        if (!value)
            continue;
        const term = document.createElement("dt");
        term.textContent = label;
        const description = document.createElement("dd");
        description.textContent = value;
        facts.append(term, description);
    }
    if (!media) {
        const empty = document.createElement("dd");
        empty.textContent = "A media file has not been recovered for this work.";
        facts.append(empty);
    }
}
async function load() {
    try {
        show(await send());
        sessionStorage.setItem("cueporchOperator", key.value);
        identify.hidden = true;
        pane.hidden = false;
        status.textContent = "";
    }
    catch (error) {
        status.textContent = String(error);
    }
}
function lines(name) {
    return input(name).value.split("\n").map((value) => value.trim()).filter(Boolean);
}
identify.onsubmit = (event) => { event.preventDefault(); void load(); };
form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
        const body = {
            title: input("title").value,
            description: input("description").value,
            tags: lines("tags"),
            categories: lines("categories"),
            credits: lines("credits").map((value) => {
                const [name, ...role] = value.split("|");
                return { name: name.trim(), role: role.join("|").trim() };
            }),
            license: input("license").value || null,
            rating: rating.value,
            content: new FormData(form).getAll("content"),
            realistic_ai: input("realistic_ai").checked,
            advertisement: input("advertisement").checked,
        };
        show(await send("PUT", body));
        status.textContent = "Video details saved.";
    }
    catch (error) {
        status.textContent = String(error);
    }
    finally {
        submit.disabled = false;
    }
};
if (key.value)
    void load();
export {};
