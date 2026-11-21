import {checked, sessionMarker, teamRequest} from "./team_api.js";

type Vote = "up" | "down";
interface Preferences {votes: Record<string, Vote>; can_manage: boolean}

const credential = document.body.dataset.updatesTeam === "yes"
  ? sessionMarker : sessionStorage.getItem("cueporchOperator") || "";

function enableFeedback(card: HTMLElement, prior: Vote | undefined) {
  const controls = card.querySelector<HTMLElement>("[data-update-votes]")!;
  const buttons = Array.from(controls.querySelectorAll<HTMLButtonElement>("button[data-vote]"));
  const status = controls.querySelector<HTMLElement>("[role=status]")!;
  const path = `updates/${encodeURIComponent(card.dataset.update!)}/feedback`;
  let vote: Vote | null = prior || null;
  const paint = () => {
    for (const button of buttons) button.setAttribute("aria-pressed", String(vote === button.dataset.vote));
  };
  paint();
  controls.hidden = false;
  for (const button of buttons) {
    button.onclick = async () => {
      const candidate = button.dataset.vote as Vote;
      const removing = candidate === vote;
      for (const choice of buttons) choice.disabled = true;
      try {
        const response = await checked(await teamRequest(path, removing ? "DELETE" : "PUT", {vote: candidate}, credential));
        vote = (await response.json()).vote;
        status.textContent = removing ? "Feedback removed." : "Feedback saved.";
        paint();
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        for (const choice of buttons) choice.disabled = false;
      }
    };
  }
}

async function loadPreferences() {
  const response = await teamRequest("update-feedback", "GET", {}, credential);
  if (!response.ok) return;
  const data: Preferences = await response.json();
  document.querySelector<HTMLElement>("#updatesFeedback")!.hidden = false;
  document.querySelector<HTMLElement>("#manageUpdates")!.hidden = !data.can_manage;
  for (const card of document.querySelectorAll<HTMLElement>("[data-update]")) {
    enableFeedback(card, data.votes[card.dataset.update!]);
  }
}

if (credential) void loadPreferences().catch(() => { /* Public announcements stay readable. */ });
