interface PublicVideo { id: string; title: string; }
interface TimedCard { start: number; title: string; text: string; link: string; image: string | null; }
interface EndScreen { mode: string; title?: string; text?: string; label?: string; link?: string; image?: string | null; videos?: string[]; }
interface Settings { cards: TimedCard[]; ending: EndScreen; }
interface Presentation { version: string; settings: Settings; public_videos: PublicVideo[]; }
type VideoRequest = <T>(suffix: string, method?: string, body?: object) => Promise<T>;

export function setupPresentation(
  call: VideoRequest,
  addPicture: (file: File) => Promise<string>,
  timestamp: (value: string) => number,
  displayTime: (seconds: number) => string,
  preview: HTMLVideoElement,
) {
  const form = document.querySelector<HTMLFormElement>("#presentationEditor")!;
  const status = document.querySelector<HTMLElement>("#presentationStatus")!;
  const rows = document.querySelector<HTMLOListElement>("#cardRows")!;
  const add = document.querySelector<HTMLButtonElement>("#addTimedCard")!;
  const save = document.querySelector<HTMLButtonElement>("#savePresentation")!;
  const end = document.querySelector<HTMLFieldSetElement>("#ending")!;
  const videos = document.querySelector<HTMLElement>("#endVideos")!;
  const template = document.querySelector<HTMLTemplateElement>("#cardRow")!;
  const mode = end.querySelector<HTMLSelectElement>("[name=mode]")!;
  const field = (node: Element, name: string) => node.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[name="${name}"]`)!;
  let current: Presentation;

  function manageImage(node: HTMLElement, digest?: string | null) {
    const image = node.querySelector<HTMLInputElement>("[name=image]")!;
    const caption = node.querySelector<HTMLElement>("[data-image-state]")!;
    image.dataset.digest = digest || "";
    caption.textContent = digest ? "Image saved" : "";
    node.querySelector<HTMLButtonElement>("[data-remove-image]")!.onclick = () => {
      image.value = "";
      image.dataset.digest = "";
      caption.textContent = "";
    };
  }

  async function savedImage(node: Element) {
    const input = node.querySelector<HTMLInputElement>("[name=image]")!;
    const file = input.files?.[0];
    if (file) {
      input.dataset.digest = await addPicture(file);
      input.value = "";
      node.querySelector<HTMLElement>("[data-image-state]")!.textContent = "Image saved";
    }
    return input.dataset.digest || null;
  }

  function appendCard(card: TimedCard) {
    const row = template.content.firstElementChild!.cloneNode(true) as HTMLLIElement;
    field(row, "start").value = displayTime(card.start);
    field(row, "title").value = card.title;
    field(row, "text").value = card.text;
    field(row, "link").value = card.link;
    manageImage(row, card.image);
    row.querySelector<HTMLButtonElement>("[data-remove-card]")!.onclick = () => {
      row.remove();
      add.disabled = false;
    };
    rows.append(row);
    add.disabled = rows.children.length >= 100;
    return row;
  }

  function visibleEndControls() {
    for (const item of end.querySelectorAll<HTMLElement>("[data-mode]")) {
      item.hidden = !item.dataset.mode!.split(" ").includes(mode.value);
      for (const input of item.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input,textarea")) {
        input.disabled = item.hidden;
      }
    }
  }

  function render(value: Presentation) {
    current = value;
    rows.replaceChildren();
    add.disabled = false;
    current.settings.cards.forEach(appendCard);
    const ending = current.settings.ending;
    mode.value = ending.mode;
    for (const key of ["title", "text", "label", "link"] as const) field(end, key).value = ending[key] || "";
    manageImage(end, ending.image);
    videos.replaceChildren();
    for (const video of value.public_videos) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "videos";
      input.value = video.id;
      input.checked = ending.videos?.includes(video.id) || false;
      label.append(input, " " + video.title);
      videos.append(label);
    }
    visibleEndControls();
  }

  mode.onchange = visibleEndControls;
  add.onclick = () => {
    const row = appendCard({start: Math.floor(preview.currentTime || 0), title: "", text: "", link: "", image: null});
    field(row, "title").focus();
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    save.disabled = true;
    status.textContent = "";
    try {
      const cards = [];
      for (const row of rows.children) {
        cards.push({start: timestamp(field(row, "start").value), title: field(row, "title").value,
          text: field(row, "text").value, link: field(row, "link").value, image: await savedImage(row)});
      }
      const ending: EndScreen = {mode: mode.value};
      for (const key of ["title", "text", "label", "link"] as const) ending[key] = field(end, key).value;
      if (!end.querySelector<HTMLInputElement>("[name=image]")!.disabled) ending.image = await savedImage(end);
      if (ending.mode === "more") {
        ending.videos = Array.from(videos.querySelectorAll<HTMLInputElement>(":checked"), (input) => input.value);
      }
      render(await call<Presentation>("/presentation", "PUT", {
        version: current.version, previous: current.settings, cards, ending,
      }));
      status.textContent = "Cards and end screen saved.";
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      save.disabled = false;
    }
  };

  return async () => {
    form.hidden = true;
    try {
      render(await call<Presentation>("/presentation"));
      form.hidden = false;
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : String(error);
      form.hidden = false;
      save.disabled = true;
      add.disabled = true;
    }
  };
}
