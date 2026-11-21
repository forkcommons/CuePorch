import { checked, sessionMarker, teamRequest } from "./team_api.js";
const credential = document.body.dataset.updatesTeam === "yes"
    ? sessionMarker : sessionStorage.getItem("cueporchOperator") || "";
function enableFeedback(card, prior) {
    const controls = card.querySelector("[data-update-votes]");
    const buttons = Array.from(controls.querySelectorAll("button[data-vote]"));
    const status = controls.querySelector("[role=status]");
    const path = `updates/${encodeURIComponent(card.dataset.update)}/feedback`;
    let vote = prior || null;
    const paint = () => {
        for (const button of buttons)
            button.setAttribute("aria-pressed", String(vote === button.dataset.vote));
    };
    paint();
    controls.hidden = false;
    for (const button of buttons) {
        button.onclick = async () => {
            const candidate = button.dataset.vote;
            const removing = candidate === vote;
            for (const choice of buttons)
                choice.disabled = true;
            try {
                const response = await checked(await teamRequest(path, removing ? "DELETE" : "PUT", { vote: candidate }, credential));
                vote = (await response.json()).vote;
                status.textContent = removing ? "Feedback removed." : "Feedback saved.";
                paint();
            }
            catch (error) {
                status.textContent = error instanceof Error ? error.message : String(error);
            }
            finally {
                for (const choice of buttons)
                    choice.disabled = false;
            }
        };
    }
}
async function loadPreferences() {
    const response = await teamRequest("update-feedback", "GET", {}, credential);
    if (!response.ok)
        return;
    const data = await response.json();
    document.querySelector("#updatesFeedback").hidden = false;
    document.querySelector("#manageUpdates").hidden = !data.can_manage;
    for (const card of document.querySelectorAll("[data-update]")) {
        enableFeedback(card, data.votes[card.dataset.update]);
    }
}
if (credential)
    void loadPreferences().catch(() => { });
