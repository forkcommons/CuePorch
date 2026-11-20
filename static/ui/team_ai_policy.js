import { checked, sessionMarker, teamRequest } from "./team_api.js";
const access = document.querySelector("#aiAccess");
const key = access.querySelector("input");
const form = document.querySelector("#aiSettings");
const status = document.querySelector("#aiStatus");
const save = form.querySelector("button[type=submit]");
const usage = document.querySelector("#aiUsage");
const dictionary = document.querySelector("#aiDictionary");
const questions = document.querySelector("#enableViewerQuestions");
const workflows = Array.from(form.querySelectorAll("[data-feature]"), row => ({
    feature: row.dataset.feature,
    enabled: row.querySelector("[name=enabled]"),
    administrators: row.querySelector("[name=administrators]"),
}));
let revision = "";
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
function show(policy) {
    revision = policy.revision;
    for (const row of workflows) {
        const rule = policy.settings.features[row.feature];
        row.enabled.checked = rule.enabled;
        row.administrators.checked = rule.administrators;
    }
    dictionary.value = policy.settings.dictionary.map(({ language, source, preferred }) => [language, source, preferred].join("\t")).join("\n");
    questions.checked = policy.settings.viewer_questions;
    access.hidden = true;
    form.hidden = false;
}
function fail(error) {
    status.textContent = error instanceof Error ? error.message : String(error);
}
async function open() {
    try {
        const response = await checked(await teamRequest("ai", "GET", {}, credential));
        show(await response.json());
        if (credential !== sessionMarker)
            sessionStorage.setItem("cueporchOperator", credential);
        status.textContent = "";
    }
    catch (error) {
        fail(error);
    }
}
access.onsubmit = event => {
    event.preventDefault();
    credential = key.value;
    access.reset();
    void open();
};
form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
        const entries = dictionary.value.split("\n").filter(line => line.trim()).map(line => {
            const columns = line.split("\t").map(value => value.trim());
            if (columns.length !== 3 || columns.some(value => !value)) {
                throw new Error("Use three tab-separated fields on each dictionary line.");
            }
            const [language, source, preferred] = columns;
            return { language, source, preferred };
        });
        const features = Object.fromEntries(workflows.map(row => [
            row.feature, { enabled: row.enabled.checked, administrators: row.administrators.checked },
        ]));
        const response = await checked(await teamRequest("ai", "PUT", {
            revision, settings: { features, dictionary: entries, viewer_questions: questions.checked },
        }, credential));
        show(await response.json());
        status.textContent = "AI settings saved.";
    }
    catch (error) {
        fail(error);
    }
    finally {
        save.disabled = false;
    }
};
usage.onclick = async () => {
    usage.disabled = true;
    try {
        const response = await checked(await teamRequest("ai/usage", "GET", {}, credential));
        const link = document.createElement("a");
        link.href = URL.createObjectURL(await response.blob());
        link.download = "cueporch-ai-usage.csv";
        link.click();
        window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    }
    catch (error) {
        fail(error);
    }
    finally {
        usage.disabled = false;
    }
};
void open();
