import {viewerRequest} from "./audience_api.js";

const feedFeedback = document.querySelector<HTMLElement>("#viewerFeed")?.dataset;
const updateError = feedFeedback?.updateError || "Could not update your follows.";
const loadError = feedFeedback?.loadError || "Could not load your follow status.";
const creatorError = feedFeedback?.creatorError || "Could not unfollow creator.";
const saveError = feedFeedback?.saveError || "Could not save preferences.";

interface FollowCategoryState {
  following: boolean;
}

for (const widget of document.querySelectorAll<HTMLElement>("[data-follow-category], [data-follow-channel], [data-follow-group]")) {
  const button = widget.querySelector<HTMLButtonElement>("button")!;
  const signIn = widget.querySelector<HTMLAnchorElement>("a")!;
  const status = widget.querySelector<HTMLElement>("[role=status]")!;
  const kind = widget.dataset.followGroup !== undefined ? "group" :
    widget.dataset.followChannel !== undefined ? "channel" : "category";
  const name = widget.dataset.followGroup ?? widget.dataset.followChannel ?? widget.dataset.followCategory ?? "";
  const isPresetRemove = widget.dataset.feedRemove === "true";

  async function update(method: "GET" | "PUT" | "DELETE") {
    status.textContent = "";
    button.disabled = true;
    try {
      let response: Response;
      if (method === "GET") {
        const params = new URLSearchParams({ name });
        response = await viewerRequest(`feed/${kind}?${params.toString()}`, "GET");
      } else {
        response = await viewerRequest(`feed/${kind}`, method, { name });
      }

      if (response.status === 401) {
        button.hidden = true;
        signIn.hidden = false;
        return;
      }

      if (!response.ok) {
        if (method !== "GET") status.textContent = updateError;
        return;
      }

      if (isPresetRemove) {
        if (method === "DELETE") {
          window.location.reload();
        }
        return;
      }

      const state: FollowCategoryState = await response.json();
      button.setAttribute("aria-pressed", String(state.following));
      button.textContent = state.following ? `Unfollow ${kind}` : `Follow ${kind}`;
      button.hidden = false;
      signIn.hidden = true;
    } catch {
      status.textContent = loadError;
    } finally {
      button.disabled = false;
    }
  }

  if (isPresetRemove) {
    button.hidden = false;
    signIn.hidden = true;
    button.setAttribute("aria-pressed", "true");
    button.onclick = () => void update("DELETE");
  } else {
    button.hidden = true;
    signIn.hidden = true;
    button.onclick = () => void update(button.getAttribute("aria-pressed") === "true" ? "DELETE" : "PUT");
    void update("GET");
  }
}

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-feed-unfollow-creator]")) {
  const handle = button.dataset.feedUnfollowCreator || "";
  const status = button.parentElement?.querySelector<HTMLElement>("[role=status]") ?? null;

  button.onclick = async () => {
    if (status) status.textContent = "";
    button.disabled = true;
    try {
      const response = await viewerRequest("follow/" + encodeURIComponent(handle), "DELETE");
      if (!response.ok) {
        if (status) status.textContent = creatorError;
        return;
      }
      window.location.reload();
    } catch {
      if (status) status.textContent = creatorError;
    } finally {
      button.disabled = false;
    }
  };
}

for (const form of document.querySelectorAll<HTMLFormElement>("[data-creator-activity]")) {
  const handle = form.dataset.creatorActivity || "";
  const status = form.querySelector<HTMLElement>("[role=status]")!;
  const save = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
  form.onsubmit = async event => {
    event.preventDefault();
    status.textContent = "";
    save.disabled = true;
    try {
      const fields = new FormData(form);
      const response = await viewerRequest("feed/creator/" + encodeURIComponent(handle), "PUT", {
        videos: fields.has("videos"), appearances: fields.has("appearances"), likes: fields.has("likes"),
      });
      if (!response.ok) {
        status.textContent = saveError;
        return;
      }
      window.location.reload();
    } catch {
      status.textContent = saveError;
    } finally {
      save.disabled = false;
    }
  };
}
