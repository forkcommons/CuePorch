const key = document.querySelector("input#key");
key.value = sessionStorage.getItem("cueporchOperator") || "";
const source = document.querySelector("input#source");
const inventory = document.querySelector("button#inventory");
const transfer = document.querySelector("button#transfer");
const rows = document.querySelector("tbody#rows");
const showcases = document.querySelector("#showcaseInventory");
const showcaseIssue = document.querySelector("#showcaseInventoryIssue");
const showcaseRows = document.querySelector("#showcaseInventoryRows");
const headers = () => ({
    Authorization: "Bearer " + key.value,
    "Content-Type": "application/json",
});
async function run(dry) {
    inventory.disabled = transfer.disabled = true;
    statusBox.textContent = "Starting migration request…";
    try {
        const response = await fetch("/api/migration", {
            method: "POST",
            headers: headers(),
            body: JSON.stringify({ dry_run: dry, token: source.value }),
        });
        if (!response.ok)
            throw new Error(`Request failed (${response.status})`);
        await poll();
    }
    catch (error) {
        failed(error);
    }
}
const statusBox = document.querySelector("p#status");
function failed(error) {
    inventory.disabled = transfer.disabled = false;
    statusBox.textContent = error instanceof Error ? error.message : String(error);
}
async function poll() {
    const r = await fetch("/api/migration", { headers: headers() });
    if (!r.ok)
        throw new Error(`Could not read migration progress (${r.status})`);
    const data = await r.json();
    statusBox.textContent = data.state + (data.detail ? " · " + data.detail : "");
    rows.replaceChildren();
    for (const w of data.works || []) {
        const row = rows.insertRow();
        for (const v of [w.title, w.source_id, w.disposition, w.source_privacy])
            row.insertCell().textContent = v;
    }
    const albums = data.showcases || [];
    showcases.hidden = !albums.length && !data.showcase_issue;
    showcaseIssue.textContent = data.showcase_issue || "";
    showcaseRows.replaceChildren();
    for (const album of albums) {
        const row = showcaseRows.insertRow();
        for (const value of [album.title, album.source_visibility, String(album.members.length), album.missing.join(", ")]) {
            row.insertCell().textContent = value;
        }
        const target = row.insertCell();
        if (album.target) {
            const link = document.createElement("a");
            link.href = "/operator/showcases?select=" + encodeURIComponent(album.target);
            link.textContent = "Open showcase";
            target.append(link);
        }
        else {
            target.textContent = data.state === "transferred" ? "Removed locally" : "Private on transfer";
        }
        if (album.issues.length)
            target.append(" " + album.issues.join(" "));
    }
    if (["running", "waiting"].includes(data.state))
        setTimeout(() => void poll().catch(failed), 750);
    else
        inventory.disabled = transfer.disabled = false;
}
inventory.onclick = () => run(true);
transfer.onclick = () => run(false);
export {};
