export interface TextLayer {
  text: string;
  style: string;
  font: string;
  size: number;
  color: string;
  fill: string;
  highlight: string;
  align: string;
  position: string;
  shadow: boolean;
  effect: string;
}

const EFFECTS = ["Cut", "Fade", "Slide", "Reveal", "Typewriter", "Flicker", "Scale",
  "Underline", "Lines", "Frame", "Cutout", "Wipe", "Circle", "Capsule", "Shadow", "Rounded"];
const STYLE_SIZES: Record<string, number> = { title: 10, headline: 8, subheadline: 6, caption: 4 };
const defaults: TextLayer = {
  text: "", style: "title", font: "sans", size: 10, color: "ffffff",
  fill: "", highlight: "", align: "center", position: "center", shadow: false, effect: "cut",
};

export function mountTimelineText(receive: (layers: TextLayer[]) => void) {
  const el = <T extends HTMLElement>(suffix: string) => document.getElementById("timelineLayer" + suffix) as T;
  const sel = el<HTMLSelectElement>("Select");
  const text = el<HTMLTextAreaElement>("Text");
  const style = el<HTMLSelectElement>("Style");
  const font = el<HTMLSelectElement>("Font");
  const size = el<HTMLInputElement>("Size");
  const color = el<HTMLInputElement>("Color");
  const fillOn = el<HTMLInputElement>("FillOn");
  const fill = el<HTMLInputElement>("Fill");
  const highlightOn = el<HTMLInputElement>("HighlightOn");
  const highlight = el<HTMLInputElement>("Highlight");
  const align = el<HTMLSelectElement>("Align");
  const position = el<HTMLSelectElement>("Position");
  const shadow = el<HTMLInputElement>("Shadow");
  const effect = el<HTMLSelectElement>("Effect");
  const apply = document.getElementById("applyTimelineText") as HTMLButtonElement;
  const remove = document.getElementById("clearTimelineText") as HTMLButtonElement;
  const fields = [sel, text, style, font, size, color, fillOn, highlightOn, align, position, shadow, effect];

  effect.replaceChildren(...EFFECTS.map(label => new Option(label, label.toLowerCase())));
  let layers: TextLayer[] = [];
  let active = -1;
  let enabled = false;
  let lastKey = "";

  function syncDisabled() {
    for (const field of fields) field.disabled = !enabled;
    fill.disabled = !enabled || !fillOn.checked;
    highlight.disabled = !enabled || !highlightOn.checked;
    apply.disabled = !enabled || (active === -1 && layers.length >= 4) || !text.value.trim() || !size.validity.valid || !size.value;
    remove.disabled = !enabled || active === -1;
  }

  function populate(layer: TextLayer) {
    text.value = layer.text;
    style.value = layer.style;
    font.value = layer.font;
    size.value = String(layer.size);
    color.value = "#" + layer.color;
    fillOn.checked = Boolean(layer.fill);
    fill.value = "#" + (layer.fill || "182629");
    highlightOn.checked = Boolean(layer.highlight);
    highlight.value = "#" + (layer.highlight || "e5a323");
    align.value = layer.align;
    position.value = layer.position;
    shadow.checked = layer.shadow;
    effect.value = layer.effect;
    syncDisabled();
  }

  function show(next: TextLayer[], allowed: boolean) {
    const key = JSON.stringify([next, allowed]);
    if (key === lastKey) return;
    lastKey = key;
    layers = next;
    enabled = allowed;
    if (active >= layers.length) active = -1;
    sel.replaceChildren(new Option("New text layer", ""),
      ...layers.map((layer, index) => new Option(layer.text.replace(/\s+/g, " ").slice(0, 36), String(index))));
    sel.value = active < 0 ? "" : String(active);
    populate(layers[active] ?? defaults);
  }

  sel.onchange = () => {
    active = sel.value === "" ? -1 : Number(sel.value);
    populate(layers[active] ?? defaults);
  };
  style.onchange = () => {
    size.value = String(STYLE_SIZES[style.value]);
    position.value = style.value === "caption" ? "bottom" : "center";
    syncDisabled();
  };
  for (const field of [text, size, fillOn, highlightOn]) field.oninput = syncDisabled;

  apply.onclick = () => {
    if (apply.disabled) return;
    const layer: TextLayer = {
      text: text.value.trim(), style: style.value, font: font.value, size: Number(size.value),
      color: color.value.slice(1), fill: fillOn.checked ? fill.value.slice(1) : "",
      highlight: highlightOn.checked ? highlight.value.slice(1) : "", align: align.value,
      position: position.value, shadow: shadow.checked, effect: effect.value,
    };
    if (active === -1) {
      active = layers.length;
      receive([...layers, layer]);
    } else {
      receive(layers.map((existing, index) => index === active ? layer : existing));
    }
  };
  remove.onclick = () => {
    if (remove.disabled) return;
    const index = active;
    active = -1;
    receive(layers.filter((_, cursor) => cursor !== index));
  };
  show([], false);
  return {show};
}
