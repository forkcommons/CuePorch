const key = document.querySelector("input#key");
key.value = sessionStorage.getItem("cueporchOperator") || "";
const source = document.querySelector("input#source");
const inventory = document.querySelector("button#inventory");
const transfer = document.querySelector("button#transfer");
const rows = document.querySelector("tbody#rows");
const headers = () => ({
    Authorization: "Bearer " + key.value,
    "Content-Type": "application/json",
});
async function run(dry) {
    const r = await fetch("/api/migration", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ dry_run: dry, token: source.value }),
    });
    if (!r.ok) {
        statusBox.textContent = "Request failed (" + r.status + ")";
        return;
    }
    poll();
}
const statusBox = document.querySelector("p#status");
async function poll() {
    const r = await fetch("/api/migration", { headers: headers() });
    if (!r.ok)
        return;
    const data = await r.json();
    statusBox.textContent = data.state + (data.detail ? " · " + data.detail : "");
    rows.replaceChildren();
    for (const w of data.works || []) {
        const row = rows.insertRow();
        for (const v of [w.title, w.source_id, w.disposition, w.source_privacy])
            row.insertCell().textContent = v;
    }
    if (["running", "waiting"].includes(data.state))
        setTimeout(poll, 750);
}
inventory.onclick = () => run(true);
transfer.onclick = () => run(false);
export {};
