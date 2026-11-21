import {checked, sessionMarker, teamRequest} from "./team_api.js";

interface Item {
  id: string;
  title: string;
  label: string;
  url: string;
  at: number;
  read: boolean;
}

interface ActivityPage {items: Item[]; more: boolean}

for (const panel of document.querySelectorAll<HTMLDetailsElement>(".workspace-activity")) {
  const list = panel.querySelector<HTMLUListElement>("[data-activity-items]")!;
  const status = panel.querySelector<HTMLElement>("[data-activity-status]")!;
  const more = panel.querySelector<HTMLButtonElement>("[data-activity-more]")!;
  const credential = () => panel.dataset.activityMode === "team" ? sessionMarker
    : sessionStorage.getItem("cueporchOperator") || "";
  let active = 0;
  let cursor: Item | undefined;

  function row(item: Item, generation: number) {
    const entry = document.createElement("li");
    const name = document.createElement("a");
    name.href = item.url;
    name.textContent = item.title;
    const detail = document.createElement("span");
    detail.textContent = item.label;
    const when = document.createElement("time");
    const date = new Date(item.at * 1000);
    when.dateTime = date.toISOString();
    when.textContent = date.toLocaleString();
    const read = document.createElement("span");
    read.textContent = "Read";
    const mark = document.createElement("button");
    mark.type = "button";
    mark.textContent = "Mark as read";
    mark.setAttribute("aria-label", `Mark as read: ${item.title} · ${item.label}`);
    mark.onclick = async () => {
      mark.disabled = true;
      try {
        await checked(await teamRequest("activity", "PUT", {id: item.id}, credential()));
        mark.replaceWith(read);
        if (active === generation && panel.open) {
          name.focus();
          status.textContent = "Activity marked as read.";
        }
      } catch (error) {
        if (active === generation) status.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        mark.disabled = false;
      }
    };
    entry.append(name, detail, when, item.read ? read : mark);
    return entry;
  }

  function reset() {
    active += 1;
    cursor = undefined;
    list.replaceChildren();
    status.textContent = "";
    more.hidden = true;
    more.disabled = false;
  }

  async function display(older = false) {
    const generation = active;
    const key = credential();
    if (!key) {
      status.textContent = "Open the workspace with an operator credential to see activity.";
      return;
    }
    more.disabled = true;
    status.textContent = "Loading activity…";
    try {
      const query = cursor ? "?" + new URLSearchParams({at: String(cursor.at), before: cursor.id}) : "";
      const response = await checked(await teamRequest("activity" + query, "GET", {}, key));
      const page: ActivityPage = await response.json();
      if (generation !== active || !panel.open) return;
      const rows = page.items.map(item => row(item, generation));
      cursor = page.items.at(-1) ?? cursor;
      list.append(...rows);
      more.hidden = !page.more;
      status.textContent = list.children.length ? "" : "No activity in the last 30 days.";
      if (older) rows[0]?.querySelector("a")?.focus();
    } catch (error) {
      if (generation === active) status.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      if (generation === active) more.disabled = false;
    }
  }

  more.onclick = () => void display(true);
  panel.addEventListener("toggle", () => {
    reset();
    if (panel.open) void display();
  });
  window.addEventListener("pagehide", () => {
    panel.open = false;
    reset();
  });
}
