const key = document.querySelector<HTMLInputElement>("input#key")!;
key.value = sessionStorage.getItem("cueporchOperator") || "";
const load = document.querySelector<HTMLButtonElement>("button#load")!;
const settings = document.querySelector<HTMLElement>("div#settings")!;
const policyLabels: Record<string, string> = {
  public_comments: "public comments", ai_details: "AI video details", ai_chapters: "AI chapters", ai_script: "AI scripts",
  ai_highlights: "AI highlights",
  event_contact_sync: "HubSpot registration delivery",
  event_engagement_sync: "HubSpot engagement delivery",
};
const queue = document.querySelector<HTMLElement>("div#queue")!;
const result = document.querySelector<HTMLElement>("p#result")!;
const moderation = document.querySelector<HTMLElement>("#public-comments")!;
const moderationSearch = moderation.querySelector<HTMLFormElement>("form")!;
const moderationList = moderation.querySelector<HTMLUListElement>("ul")!;
const moderationStatus = moderation.querySelector<HTMLElement>("[role=status]")!;
const moderationMore = moderation.querySelector<HTMLButtonElement>("[data-more-comments]")!;
let moderationPage = 1;
let moderationQuery = "";
let moderationVersion = 0;
const call = (url: string, opt: RequestInit = {}) =>
  fetch(url, {
    ...opt,
    headers: {
      Authorization: "Bearer " + key.value,
      "Content-Type": "application/json",
    },
  });

async function publishedComments(page = 1, notice = "") {
  const version = ++moderationVersion;
  moderationMore.disabled = true;
  try {
    const params = new URLSearchParams({q: moderationQuery, page: String(page)});
    const response = await call(`/api/operator/public-comments?${params}`);
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (version !== moderationVersion) return;
    if (page === 1) moderationList.replaceChildren();
    for (const comment of data.comments) {
      const item = document.createElement("li");
      const video = document.createElement("a");
      video.href = `/watch/${encodeURIComponent(comment.kind)}/${encodeURIComponent(comment.video)}`;
      video.textContent = "Open video";
      const message = document.createElement("p");
      message.className = "watch-comment-body";
      message.textContent = `${comment.author}: ${comment.body}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove comment";
      remove.onclick = async () => {
        remove.disabled = true;
        try {
          const deleted = await call(`/api/operator/public-comments/${comment.id}`, {method: "DELETE"});
          if (!deleted.ok) throw new Error();
          await publishedComments(1, "Comment removed.");
        } catch {
          moderationStatus.textContent = "Could not remove the comment. Try again.";
        } finally { remove.disabled = false; }
      };
      item.append(message, video, " ", remove);
      moderationList.append(item);
    }
    moderationPage = page;
    moderationMore.hidden = !data.more;
    moderationStatus.textContent = notice || (moderationList.children.length ? "" : "No published comments found.");
  } catch {
    if (version === moderationVersion) {
      moderationList.replaceChildren();
      moderationMore.hidden = true;
      moderationStatus.textContent = "Could not load published comments.";
    }
  } finally { if (version === moderationVersion) moderationMore.disabled = false; }
}

moderationSearch.onsubmit = event => {
  event.preventDefault();
  moderationQuery = String(new FormData(moderationSearch).get("q"));
  void publishedComments();
};
moderationMore.onclick = () => void publishedComments(moderationPage + 1);

load.onclick = async () => {
  const response = await call("/api/policy");
  if (!response.ok) {
    result.textContent = "Access denied";
    settings.replaceChildren();
    queue.replaceChildren();
    moderation.hidden = true;
    ++moderationVersion;
    return;
  }
  settings.replaceChildren();
  for (const [step, required] of Object.entries(await response.json())) {
    const label = document.createElement("label"),
      input = document.createElement("input");
    input.type = "checkbox";
    input.checked = required === true;
    input.onchange = async () => {
      let r = await call("/api/policy/" + step, {
        method: "PUT",
        body: JSON.stringify({ required: input.checked }),
      });
      result.textContent = r.ok ? "Saved" : "Unable to save";
      if (!r.ok) input.checked = !input.checked;
      else if (step === "public_comments" && !input.checked) load.click();
    };
    label.append(
      input,
      document.createTextNode("Require approval for " + (policyLabels[step] || step)),
    );
    settings.append(label, document.createElement("br"));
  }
  queue.replaceChildren();
  for (const job of await (await call("/api/actions")).json()) {
    const line = document.createElement("p");
    if (job.comment_body !== undefined) {
      const message = document.createElement("span");
      message.className = "watch-comment-body";
      message.textContent = `Public comment by ${job.comment_author}: ${job.comment_body} `;
      line.append(message);
    } else line.append(document.createTextNode(job.step + " " + job.payload + " "));
    for (const decision of ["approve", "reject"]) {
      const button = document.createElement("button");
      button.textContent = decision;
      button.onclick = async () => {
        const r = await call("/api/actions/" + job.id + "/decision", {
          method: "POST",
          body: JSON.stringify({ decision }),
        });
        if (r.ok) load.click();
        else result.textContent = "Decision failed";
      };
      line.append(button);
    }
    queue.append(line);
  }
  moderation.hidden = false;
  await publishedComments();
};
