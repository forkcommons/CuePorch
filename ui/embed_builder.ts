/** Produces accessible iframe markup from the current video's permitted embed URL. */

function attribute(value: string) {
  return value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

export function embedBuilder() {
  const controls = document.querySelector<HTMLFieldSetElement>("#embedOptions")!;
  const code = document.querySelector<HTMLTextAreaElement>("#workspaceEmbed")!;
  const label = document.querySelector<HTMLElement>("#workspaceEmbedLabel")!;
  const copy = document.querySelector<HTMLButtonElement>("#copyEmbed")!;
  const scriptLabel = document.querySelector<HTMLElement>("#embedApiOption")!;
  const input = (name: string) => controls.querySelector<HTMLInputElement>(`[name=${name}]`)!;
  let path: URL | null;
  let title: string;
  let native = false;

  function update() {
    if (!path) return;
    const url = new URL(path);
    for (const name of ["autoplay", "muted", "loop"]) {
      if (input(name).checked) url.searchParams.set(name, "1");
    }
    const useApi = native && input("api").checked;
    if (useApi) url.searchParams.set("api", "1");
    const width = input("width");
    const height = input("height");
    if (!width.validity.valid || !height.validity.valid) {
      code.value = "";
      copy.disabled = true;
      return;
    }
    const dimensions = input("responsive").checked
      ? `style="width:100%;aspect-ratio:${width.valueAsNumber}/${height.valueAsNumber};border:0"`
      : `width="${width.valueAsNumber}" height="${height.valueAsNumber}" style="border:0"`;
    code.value = `<iframe src="${attribute(url.href)}" title="${attribute(title)}" ${dimensions} referrerpolicy="origin" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen></iframe>`;
    if (useApi) {
      code.value += `\n<script type="module" src="${attribute(new URL("/player.js", url).href)}"></script>`;
    }
    copy.disabled = false;
  }

  controls.addEventListener("input", update);
  return {
    show(url: URL | null, playerTitle: string, ownPlayer: boolean) {
      path = url;
      title = playerTitle;
      native = ownPlayer;
      controls.hidden = label.hidden = copy.hidden = !path;
      scriptLabel.hidden = !native;
      update();
    },
  };
}
