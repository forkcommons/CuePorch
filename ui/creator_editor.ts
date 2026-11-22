import {checked, sessionMarker, teamRequest} from "./team_api.js";

interface Video {id: string; title: string}
interface Draft {
  handle: string; name: string; bio: string; links: string[]; visible: boolean; cover: string | null;
  featured: string[]; revision: string; url: string | null; videos: Video[];
}

const editor = document.querySelector<HTMLElement>("#creatorEditor")!;
const form = document.querySelector<HTMLFormElement>("#creatorForm")!;
const status = document.querySelector<HTMLElement>("#creatorStatus")!;
const available = document.querySelector<HTMLSelectElement>("#creatorVideo")!;
const add = document.querySelector<HTMLButtonElement>("#creatorAdd")!;
const list = document.querySelector<HTMLOListElement>("#creatorFeatured")!;
const link = document.querySelector<HTMLAnchorElement>("#publicCreatorLink")!;
const name = form.elements.namedItem("name") as HTMLInputElement;
const handle = form.elements.namedItem("handle") as HTMLInputElement;
const bio = form.elements.namedItem("bio") as HTMLTextAreaElement;
const links = form.elements.namedItem("links") as HTMLTextAreaElement;
const visible = form.elements.namedItem("visible") as HTMLInputElement;
const cover = form.elements.namedItem("cover") as HTMLSelectElement;
const save = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
interface BlockItem {id: string; name: string; blocked: boolean}
interface BlockPage {items: BlockItem[]; more: boolean}

const blockList = document.querySelector<HTMLUListElement>("#creatorBlockList")!;
const previousBlocks = document.querySelector<HTMLButtonElement>("#creatorBlocksPrevious")!;
const nextBlocks = document.querySelector<HTMLButtonElement>("#creatorBlocksNext")!;
let draft: Draft;

function control(label: string, title: string, disabled: boolean, action: () => void) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-label", `${label} ${title}`);
  button.disabled = disabled;
  button.onclick = () => { action(); showFeatured(); };
  return button;
}

function move(index: number, delta: number) {
  const [video] = draft.featured.splice(index, 1);
  draft.featured.splice(index + delta, 0, video);
}

function showFeatured() {
  const videos = new Map(draft.videos.map(video => [video.id, video.title]));
  const options = draft.videos.filter(video => !draft.featured.includes(video.id));
  available.replaceChildren(...options.map(video => new Option(video.title, video.id)));
  add.disabled = options.length === 0 || draft.featured.length >= 24;
  list.replaceChildren(...draft.featured.map((uid, index) => {
    const item = document.createElement("li");
    const title = videos.get(uid) || uid;
    item.append(title + " ",
      control("Move up", title, index === 0, () => move(index, -1)),
      control("Move down", title, index === draft.featured.length - 1, () => move(index, 1)),
      control("Remove", title, false, () => { draft.featured.splice(index, 1); }));
    return item;
  }));
}

function show(settings: Draft) {
  draft = settings;
  name.value = draft.name;
  handle.value = draft.handle;
  bio.value = draft.bio;
  links.value = draft.links.join("\n");
  visible.checked = draft.visible;
  cover.replaceChildren(new Option("No cover", ""),
    ...draft.videos.map(video => new Option(video.title, video.id)));
  cover.value = draft.cover || "";
  link.hidden = !draft.visible || !draft.url;
  if (draft.url) link.href = draft.url;
  showFeatured();
  editor.hidden = false;
}

async function task(run: () => Promise<void>) {
  status.textContent = "";
  try {
    await run();
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

async function loadBlocks(page = 1): Promise<void> {
  const response = await checked(await teamRequest(`profile/blocks?page=${page}`));
  const {items, more}: BlockPage = await response.json();
  if (!items.length && page > 1) return loadBlocks(page - 1);
  blockList.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("li");
    empty.textContent = "No viewers to block.";
    blockList.append(empty);
  }
  for (const item of items) {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${item.blocked ? "Unblock" : "Block"} ${item.name}`;
    button.onclick = () => {
      button.disabled = true;
      void task(async () => {
        const path = `profile/blocks/${encodeURIComponent(item.id)}`;
        await checked(await teamRequest(path, item.blocked ? "DELETE" : "PUT"));
        await loadBlocks(page);
        status.textContent = item.blocked ? "Viewer unblocked." : "Viewer blocked.";
      }).finally(() => { button.disabled = false; });
    };
    row.append(button);
    blockList.append(row);
  }
  previousBlocks.hidden = page <= 1;
  nextBlocks.hidden = !more;
  previousBlocks.onclick = () => void task(() => loadBlocks(page - 1));
  nextBlocks.onclick = () => void task(() => loadBlocks(page + 1));
}

add.onclick = () => {
  if (!available.value) return;
  draft.featured.push(available.value);
  showFeatured();
};
form.onsubmit = event => {
  event.preventDefault();
  save.disabled = true;
  void task(async () => {
    const response = await checked(await teamRequest("profile", "PUT", {
      name: name.value, handle: handle.value, bio: bio.value, visible: visible.checked, cover: cover.value || null,
      links: links.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
      revision: draft.revision, featured: draft.featured,
    }));
    show(await response.json());
    status.textContent = "Creator profile saved.";
  }).finally(() => { save.disabled = false; });
};
void task(async () => {
  let settings: Draft = await (await checked(await teamRequest("profile"))).json();
  let notice = "";
  if (new URLSearchParams(location.search).get("fromVimeo") === "1") {
    if (settings.revision) {
      notice = "Your existing creator profile was left as saved.";
    } else {
      try {
        const response = await checked(await fetch("/api/migration/profile", {
          headers: {Authorization: "Bearer " + sessionMarker},
        }));
        const source: Pick<Draft, "name" | "bio" | "links"> = await response.json();
        settings = {...settings, name: source.name, bio: source.bio, links: source.links};
        notice = "Vimeo profile loaded. Choose a handle and save when ready.";
      } catch {
        notice = "To load a Vimeo draft, preview the transfer and sign in as a workspace Owner or Administrator.";
      }
    }
  }
  show(settings);
  await loadBlocks();
  if (notice) status.textContent = notice;
});
