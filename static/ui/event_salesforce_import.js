export function setupSalesforceImport(request, onRosterChanged) {
    const $ = (s) => document.querySelector(s);
    const load = $("#sfCampaignLoad");
    const more = $("#sfCampaignMore");
    const importNow = $("#sfImportNow");
    const schedule = $("#sfImportSchedule");
    const stop = $("#sfImportStop");
    const refresh = $("#sfImportRefresh");
    const select = $("#sfCampaign");
    const status = $("#sfImportStatus");
    let base = "";
    let generation = 0;
    let busy = false;
    let cursor = null;
    let receipt;
    const importPath = () => base + "/registration/salesforce";
    const syncPath = () => importPath() + "/sync";
    function updateControls() {
        const locked = busy || receipt?.available !== true;
        load.disabled = locked;
        more.disabled = locked;
        select.disabled = locked;
        importNow.disabled = locked || !select.value;
        schedule.disabled = locked || !select.value;
        refresh.disabled = busy;
        stop.disabled = busy;
        more.hidden = cursor === null;
        stop.hidden = !receipt || receipt.state === "off";
    }
    function resultText(r) {
        return `Added ${r.added}. Already registered ${r.existing}. Duplicates ${r.duplicates}. Skipped ${r.skipped}.`;
    }
    function renderReceipt(next) {
        receipt = next ?? undefined;
        const p = [];
        if (!receipt)
            p.push("Disconnected.");
        else {
            if (!receipt.available)
                p.push("Salesforce import unavailable.");
            const s = receipt.state;
            if (s === "off")
                p.push("Off.");
            else if (s === "scheduled")
                p.push("Scheduled.");
            else if (s === "retry")
                p.push("Retry pending.");
            else if (s === "complete")
                p.push("Complete.");
            else if (s === "problem")
                p.push("Problem.");
            else
                p.push(`State: ${s}.`);
            if (receipt.problem)
                p.push(receipt.problem);
            if (receipt.next_at)
                p.push(`Next attempt at ${new Date(receipt.next_at * 1000).toLocaleString()}.`);
            if (receipt.last_result)
                p.push(resultText(receipt.last_result));
        }
        status.textContent = p.join(" ");
        updateControls();
    }
    async function run(task) {
        if (!base || busy)
            return;
        const ticket = generation;
        busy = true;
        updateControls();
        try {
            await task(ticket);
        }
        catch (err) {
            if (ticket === generation)
                status.textContent = err instanceof Error ? err.message : String(err);
        }
        finally {
            if (ticket === generation) {
                busy = false;
                updateControls();
            }
        }
    }
    const PH = () => new Option("Choose a campaign", "");
    async function loadCampaigns(ticket, reset) {
        if (!reset && cursor === null)
            return;
        const saved = select.value || receipt?.campaign || "";
        if (reset) {
            select.replaceChildren(PH());
            cursor = null;
        }
        const path = "/api/integrations/salesforce/campaigns" +
            (!reset && cursor ? `?cursor=${encodeURIComponent(cursor)}` : "");
        const page = await request(path);
        if (ticket !== generation)
            return;
        const seen = new Set(Array.from(select.options).map(o => o.value));
        for (const c of page.campaigns) {
            if (!seen.has(c.id)) {
                seen.add(c.id);
                select.add(new Option(c.name, c.id));
            }
        }
        if (seen.has(saved))
            select.value = saved;
        cursor = page.next_cursor ?? null;
        updateControls();
    }
    load.addEventListener("click", () => run(t => loadCampaigns(t, true)));
    more.addEventListener("click", () => run(t => loadCampaigns(t, false)));
    select.addEventListener("change", updateControls);
    importNow.addEventListener("click", () => run(async (t) => {
        const r = await request(importPath(), "POST", { campaign: select.value }, true);
        if (t !== generation)
            return;
        status.textContent = resultText(r);
        onRosterChanged();
    }));
    schedule.addEventListener("click", () => run(async (t) => {
        const r = await request(syncPath(), "PUT", { campaign: select.value }, true);
        if (t !== generation)
            return;
        renderReceipt(r);
    }));
    stop.addEventListener("click", () => run(async (t) => {
        const r = await request(syncPath(), "DELETE", {}, true);
        if (t !== generation)
            return;
        renderReceipt(r);
    }));
    refresh.addEventListener("click", () => run(async (t) => {
        const r = await request(syncPath());
        if (t !== generation)
            return;
        renderReceipt(r);
        onRosterChanged();
    }));
    return (target) => {
        base = target;
        generation++;
        busy = false;
        cursor = null;
        receipt = undefined;
        select.replaceChildren(PH());
        status.textContent = "";
        updateControls();
        run(async (t) => {
            const r = await request(syncPath());
            if (t !== generation)
                return;
            renderReceipt(r);
        });
    };
}
