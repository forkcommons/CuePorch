export type CardLayout = "center" | "left" | "stripe";

export interface CardClip {
  card: {
    title: string;
    subtitle: string;
    background: string;
    foreground: string;
    accent: string;
    layout: CardLayout;
  };
  start: 0;
  end: number;
}

export interface TemplateItem {
  id: string;
  title: string;
  category: string;
  description: string;
  ratios: string[];
  opening: CardClip;
  closing: CardClip;
}

const RATIO_CHOICES = ["All shapes", "16:9", "1:1", "9:16"] as const;

function resolveAspect(ratio: string): number {
  const parts = ratio.split(":");
  const w = Number(parts[0]);
  const h = Number(parts[1]);
  return w > 0 && h > 0 ? w / h : 16 / 9;
}

function pickRatio(item: TemplateItem, selected: string): string {
  if (selected && selected !== "All shapes") return selected;
  return item.ratios[0] ?? "16:9";
}

export function showTimelineCard(
  container: HTMLElement,
  clip: CardClip,
  ratio: string
): void {
  container.textContent = "";

  const c = clip.card;
  const aspect = resolveAspect(ratio);
  const height = 160;
  const width = Math.max(48, Math.round(height * aspect));

  const card = document.createElement("div");
  const s = card.style;
  s.width = `${width}px`;
  s.maxWidth = "100%";
  s.height = `${height}px`;
  s.background = `#${c.background}`;
  s.color = `#${c.foreground}`;
  s.position = "relative";
  s.overflow = "hidden";
  s.borderRadius = "6px";
  s.boxSizing = "border-box";
  s.display = "flex";
  s.flexDirection = "column";
  s.justifyContent = "center";
  s.alignItems = c.layout === "center" ? "center" : "flex-start";
  s.textAlign = c.layout === "center" ? "center" : "left";
  s.padding = c.layout === "stripe" ? "6px 10px 6px 16px" : "6px 10px";
  s.fontFamily = "system-ui, -apple-system, 'Segoe UI', sans-serif";
  s.lineHeight = "1.2";

  if (c.layout === "stripe") {
    const bar = document.createElement("div");
    const bs = bar.style;
    bs.position = "absolute";
    bs.top = "0";
    bs.bottom = "0";
    bs.left = "0";
    bs.width = "6px";
    bs.background = `#${c.accent}`;
    card.appendChild(bar);
  } else {
    const line = document.createElement("div");
    const ls = line.style;
    ls.height = "3px";
    ls.width = "28px";
    ls.background = `#${c.accent}`;
    ls.borderRadius = "2px";
    ls.marginBottom = "4px";
    card.appendChild(line);
  }

  const title = document.createElement("div");
  title.textContent = c.title;
  const ts = title.style;
  ts.fontSize = "12px";
  ts.fontWeight = "700";
  ts.maxWidth = "100%";
  ts.overflowWrap = "anywhere";
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.textContent = c.subtitle;
  const ss = sub.style;
  ss.fontSize = "10px";
  ss.opacity = "0.85";
  ss.maxWidth = "100%";
  ss.whiteSpace = "nowrap";
  ss.overflow = "hidden";
  ss.textOverflow = "ellipsis";
  ss.marginTop = "3px";
  card.appendChild(sub);

  container.appendChild(card);
}

export function mountTimelineTemplates(
  items: TemplateItem[],
  apply: (opening: CardClip, closing: CardClip, ratio: string) => void,
  insert: (clip: CardClip) => void
): void {
  const searchEl = document.getElementById("timelineTemplateSearch") as HTMLInputElement | null;
  const categoryEl = document.getElementById("timelineTemplateCategory") as HTMLSelectElement | null;
  const ratioEl = document.getElementById("timelineTemplateRatio") as HTMLSelectElement | null;
  const selectEl = document.getElementById("timelineTemplate") as HTMLSelectElement | null;
  const descriptionEl = document.getElementById("timelineTemplateDescription") as HTMLElement | null;
  const openingEl = document.getElementById("timelineTemplateOpening") as HTMLElement | null;
  const closingEl = document.getElementById("timelineTemplateClosing") as HTMLElement | null;
  const applyEl = document.getElementById("timelineTemplateApply") as HTMLButtonElement | null;
  const insertOpenEl = document.getElementById("timelineTemplateInsertOpening") as HTMLButtonElement | null;
  const insertCloseEl = document.getElementById("timelineTemplateInsertClosing") as HTMLButtonElement | null;

  if (
    !searchEl || !categoryEl || !ratioEl || !selectEl || !descriptionEl ||
    !openingEl || !closingEl || !applyEl || !insertOpenEl || !insertCloseEl
  ) {
    return;
  }

  // Category dropdown (user-facing labels).
  const categories = Array.from(new Set(items.map((i) => i.category))).sort();
  categoryEl.textContent = "";
  const allCat = document.createElement("option");
  allCat.value = "__all__";
  allCat.textContent = "All categories";
  categoryEl.appendChild(allCat);
  for (const cat of categories) {
    const opt = document.createElement("option");
    opt.value = cat;
    opt.textContent = cat;
    categoryEl.appendChild(opt);
  }
  categoryEl.value = "__all__";

  // Ratio dropdown.
  ratioEl.textContent = "";
  for (const r of RATIO_CHOICES) {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r;
    ratioEl.appendChild(opt);
  }
  ratioEl.value = "All shapes";

  let currentId: string | null = null;

  const getSelected = (): TemplateItem | null =>
    items.find((i) => i.id === currentId) ?? null;

  const setControlsEnabled = (enabled: boolean): void => {
    applyEl.disabled = !enabled;
    insertOpenEl.disabled = !enabled;
    insertCloseEl.disabled = !enabled;
  };

  const renderEmpty = (): void => {
    descriptionEl.textContent = "No templates match the current filters.";
    openingEl.textContent = "";
    closingEl.textContent = "";
    selectEl.textContent = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No templates available";
    selectEl.appendChild(opt);
    selectEl.disabled = true;
    currentId = null;
    setControlsEnabled(false);
  };

  const renderSelected = (item: TemplateItem): void => {
    descriptionEl.textContent = item.description;
    const ratio = pickRatio(item, ratioEl.value);
    showTimelineCard(openingEl, item.opening, ratio);
    showTimelineCard(closingEl, item.closing, ratio);
    selectEl.disabled = false;
    setControlsEnabled(true);
  };

  const filtered = (): TemplateItem[] => {
    const query = searchEl.value.trim().toLowerCase();
    const cat = categoryEl.value;
    const ratio = ratioEl.value;
    return items.filter((item) => {
      if (query) {
        const haystack = `${item.title} ${item.category} ${item.description}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      if (cat && cat !== "__all__" && item.category !== cat) return false;
      if (ratio !== "All shapes" && !item.ratios.includes(ratio)) return false;
      return true;
    });
  };

  const rebuild = (): void => {
    const list = filtered();
    const previous = currentId ?? selectEl.value;

    selectEl.textContent = "";

    if (list.length === 0) {
      renderEmpty();
      return;
    }

    for (const item of list) {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.title;
      selectEl.appendChild(opt);
    }

    const keep = list.find((i) => i.id === previous) ?? list[0];
    currentId = keep.id;
    selectEl.value = keep.id;
    renderSelected(keep);
  };

  searchEl.addEventListener("input", rebuild);
  categoryEl.addEventListener("change", rebuild);
  ratioEl.addEventListener("change", rebuild);

  selectEl.addEventListener("change", () => {
    const item = items.find((i) => i.id === selectEl.value) ?? null;
    if (!item) return;
    currentId = item.id;
    renderSelected(item);
  });

  applyEl.addEventListener("click", () => {
    const item = getSelected();
    if (!item) return;
    apply(item.opening, item.closing, pickRatio(item, ratioEl.value));
  });

  insertOpenEl.addEventListener("click", () => {
    const item = getSelected();
    if (item) insert(item.opening);
  });

  insertCloseEl.addEventListener("click", () => {
    const item = getSelected();
    if (item) insert(item.closing);
  });

  rebuild();
}
