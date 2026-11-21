import { checked, teamRequest } from "./team_api.js";
const access = document.querySelector("#sharingPolicyAccess");
const key = access.querySelector("input");
const settings = document.querySelector("#sharingPolicy");
const save = settings.querySelector("button");
const status = document.querySelector("#sharingPolicyStatus");
const roles = Array.from(settings.querySelectorAll("[data-role]"));
const modes = ["private", "password", "unlisted", "public", "embed_only"];
const features = ["domains", "outbound", "file_links"];
let revision = "";
function checkbox(row, name) {
    return row.querySelector(`input[name=${name}]`);
}
function narrowContributorOptions() {
    const [admin, contributor] = roles;
    for (const field of [...modes, ...features].filter(name => name !== "private")) {
        const option = checkbox(contributor, field);
        option.disabled = !checkbox(admin, field).checked;
        if (option.disabled)
            option.checked = false;
    }
}
function render(policy) {
    revision = policy.revision;
    for (const row of roles) {
        const rule = policy.rules[row.dataset.role];
        for (const mode of modes)
            checkbox(row, mode).checked = rule.modes.includes(mode);
        for (const feature of features)
            checkbox(row, feature).checked = rule[feature];
    }
    narrowContributorOptions();
    access.hidden = true;
    settings.hidden = false;
}
async function load() {
    try {
        const response = await checked(await teamRequest("sharing-policy", "GET", {}, key.value));
        render(await response.json());
        sessionStorage.setItem("cueporchOperator", key.value);
        status.textContent = "";
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
}
access.onsubmit = event => { event.preventDefault(); void load(); };
roles[0].onchange = narrowContributorOptions;
settings.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    const rules = Object.fromEntries(roles.map(row => [row.dataset.role, {
            modes: modes.filter(mode => checkbox(row, mode).checked),
            ...Object.fromEntries(features.map(feature => [feature, checkbox(row, feature).checked])),
        }]));
    try {
        const response = await checked(await teamRequest("sharing-policy", "PUT", { revision, rules }, key.value));
        const updated = await response.json();
        render(updated);
        status.textContent = updated.withdrawn
            ? `Sharing restrictions saved. Videos made private: ${updated.withdrawn}.`
            : "Sharing restrictions saved.";
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        save.disabled = false;
    }
};
key.value = sessionStorage.getItem("cueporchOperator") || "";
if (key.value)
    void load();
