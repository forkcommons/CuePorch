export {};

interface FollowState {following: boolean; followers: number | null}

for (const widget of document.querySelectorAll<HTMLElement>("[data-follow-creator]")) {
  const button = widget.querySelector<HTMLButtonElement>("button")!;
  const signIn = widget.querySelector<HTMLAnchorElement>("a")!;
  const status = widget.querySelector<HTMLElement>("[role=status]")!;
  const count = document.querySelector<HTMLElement>("[data-follower-count]");
  const endpoint = "/api/viewer/follow/" + encodeURIComponent(widget.dataset.followCreator || "");

  async function update(method: "GET" | "PUT" | "DELETE") {
    status.textContent = "";
    button.disabled = true;
    try {
      const response = await fetch(endpoint, method === "GET" ? undefined : {
        method, headers: {"Content-Type": "application/json", "X-CuePorch-Viewer": "1"}, body: "{}",
      });
      if (response.status === 401) {
        button.hidden = true;
        signIn.hidden = false;
        return;
      }
      if (!response.ok) {
        if (response.status === 404) button.hidden = true;
        if (method !== "GET") status.textContent = "Could not update your follows.";
        return;
      }
      const state: FollowState = await response.json();
      button.setAttribute("aria-pressed", String(state.following));
      button.textContent = state.following ? "Unfollow creator" : "Follow creator";
      button.hidden = false;
      signIn.hidden = true;
      if (count && state.followers !== null)
        count.textContent = state.followers.toLocaleString() + (state.followers === 1 ? " follower" : " followers");
    } catch {
      status.textContent = "Could not load your follow status.";
    } finally {
      button.disabled = false;
    }
  }

  button.onclick = () => void update(button.getAttribute("aria-pressed") === "true" ? "DELETE" : "PUT");
  void update("GET");
}
