import { checked } from "./team_api.js";
const pending = ["queued", "waiting", "running"];
const messages = {
    queued: "Script generation queued.", waiting: "Waiting for script approval.",
    running: "Generating a script with the local model.", rejected: "The operator rejected this script request.",
};
export function studioScriptGenerator(credential) {
    const panel = document.querySelector("#scriptGenerator");
    const form = panel.querySelector("form");
    const script = document.querySelector("#recordScript");
    const brief = form.elements.namedItem("brief");
    const template = form.elements.namedItem("template");
    const status = panel.querySelector("[role=status]");
    const insert = panel.querySelector("#insertGeneratedScript");
    const buttons = Array.from(form.querySelectorAll("button[type=submit]"));
    let state;
    let before = script.value;
    let seen = "";
    let poll;
    let sending = false;
    function buttonsState() {
        const running = Boolean(state?.job && pending.includes(state.job.state));
        for (const button of buttons) {
            button.disabled = sending || !state?.configured || Boolean(state?.restriction) || running
                || (button.value !== "fresh" && !script.value.trim());
        }
    }
    function use(text) {
        script.value = text;
        script.dispatchEvent(new Event("input", { bubbles: true }));
        insert.hidden = true;
        status.textContent = "Generated script is in the teleprompter.";
    }
    function display(report) {
        state = report;
        clearTimeout(poll);
        const job = report.job;
        panel.hidden = false;
        buttonsState();
        status.textContent = report.restriction || (!report.configured ? "Configure a local Ollama model to generate scripts."
            : job ? job.error || messages[job.state] || "" : "");
        if (job?.state === "ready" && job.result && seen !== job.id) {
            seen = job.id;
            if (script.value === before)
                use(job.result);
            else {
                insert.hidden = false;
                status.textContent = "Script ready. Your teleprompter edits are still in place.";
            }
        }
        if (job && pending.includes(job.state))
            poll = window.setTimeout(() => void load(), 1000);
    }
    async function call(options) {
        const headers = { Authorization: "Bearer " + credential(), "Content-Type": "application/json" };
        const response = await checked(await fetch(panel.dataset.endpoint, {
            method: options ? "POST" : "GET", headers, body: options ? JSON.stringify(options) : undefined,
        }));
        return response.json();
    }
    async function load() {
        try {
            display(await call());
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : String(error);
        }
    }
    template.onchange = () => {
        const selected = template.selectedOptions[0];
        if (selected.dataset.brief && (!brief.value.trim() ||
            Array.from(template.options).some(option => option.dataset.brief === brief.value))) {
            brief.value = selected.dataset.brief;
        }
    };
    script.addEventListener("input", buttonsState);
    insert.onclick = () => { if (state.job?.result)
        use(state.job.result); };
    form.onsubmit = async (event) => {
        event.preventDefault();
        const mode = event.submitter?.value || "fresh";
        if (sending)
            return;
        before = script.value;
        insert.hidden = true;
        sending = true;
        buttonsState();
        try {
            display(await call({
                ...Object.fromEntries(new FormData(form)), mode, prior: mode === "fresh" ? undefined : script.value,
            }));
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            sending = false;
            buttonsState();
        }
    };
    return load;
}
