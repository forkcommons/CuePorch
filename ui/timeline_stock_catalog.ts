type Context = { credential(): string; folder(): string; available(id: string): Promise<void> };
type Item = { token: string; title: string; rights: { credit: string; license: string; source_url: string } };

export function mountTimelineStockCatalog(context: Context): void {
  const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
  const kind = byId<HTMLSelectElement>("catalogKind");
  const category = byId<HTMLSelectElement>("catalogCategory");
  const mood = byId<HTMLSelectElement>("catalogMood");
  const genre = byId<HTMLSelectElement>("catalogGenre");
  const type = byId<HTMLSelectElement>("catalogType");
  const query = byId<HTMLInputElement>("catalogQuery");
  const form = byId<HTMLFormElement>("catalogForm");
  const searchBtn = byId<HTMLButtonElement>("catalogSearch");
  const nextBtn = byId<HTMLButtonElement>("catalogNext");
  const list = byId<HTMLSelectElement>("catalogItems");
  const source = byId<HTMLDivElement>("catalogSource");
  const importBtn = byId<HTMLButtonElement>("catalogImport");
  const status = byId<HTMLElement>("catalogStatus");

  const disclosure = byId<HTMLDetailsElement>("catalogDisclosure");
  const items = new Map<string, Item>();
  let busy = false;
  let hasNext = false;
  let offset = 0;

  const headers = (): HeadersInit => ({ "Content-Type": "application/json", "Authorization": `Bearer ${context.credential()}` });
  const setStatus = (msg: string): void => { status.textContent = msg; };
  const setBusy = (value: boolean): void => {
    busy = value;
    [kind, category, mood, genre, type, query, list, searchBtn].forEach(el => { el.disabled = value; });
    nextBtn.disabled = value || !hasNext;
    importBtn.disabled = value || !list.value;
  };
  const clearResults = (): void => {
    list.replaceChildren();
    source.replaceChildren();
    items.clear();
    hasNext = false;
    offset = 0;
    nextBtn.disabled = true;
    importBtn.disabled = true;
  };

  const fetchFilters = async (): Promise<void> => {
    setBusy(true);
    try {
      const res = await fetch(`/api/library/stock-catalog/filters?kind=${encodeURIComponent(kind.value)}`, { headers: headers() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json() as { categories: string[]; moods: string[]; genres: string[]; types: string[] };
      const fill = (sel: HTMLSelectElement, values: string[] = []): void => {
        sel.replaceChildren();
        if (kind.value === "audio") sel.add(new Option("All", ""));
        for (const value of values) sel.add(new Option(value, value));
      };
      fill(category, data.categories); fill(mood, data.moods); fill(genre, data.genres); fill(type, data.types);
      for (const control of [mood, genre, type]) control.parentElement!.hidden = kind.value !== "audio";
      setStatus("");
    } catch (e) {
      setStatus(`Filters error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const search = async (reset: boolean): Promise<void> => {
    if (reset) clearResults();
    setBusy(true);
    try {
      const q = new URLSearchParams({ kind: kind.value, category: category.value, mood: mood.value, genre: genre.value, type: type.value, q: query.value, offset: String(offset) });
      const res = await fetch(`/api/library/stock-catalog?${q}`, { headers: headers() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json() as { items?: Item[]; next_offset?: number };
      (data.items ?? []).forEach(it => {
        const o = document.createElement("option"); o.value = it.token; o.textContent = it.title;
        items.set(it.token, it); list.append(o);
      });
      hasNext = typeof data.next_offset === "number" && data.next_offset > offset;
      if (hasNext) offset = data.next_offset!;
      setStatus(items.size ? `${items.size} results` : "No matching assets.");
    } catch (e) {
      setStatus(`Search error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onFilter = (isKind: boolean) => (): void => { clearResults(); if (isKind) void fetchFilters(); };
  kind.addEventListener("change", onFilter(true));
  [category, mood, genre, type].forEach(el => el.addEventListener("change", onFilter(false)));
  query.addEventListener("input", onFilter(false));

  form.addEventListener("submit", e => { e.preventDefault(); if (!busy) void search(true); });
  nextBtn.addEventListener("click", () => { if (!busy && hasNext) void search(false); });

  list.addEventListener("change", () => {
    const it = items.get(list.value);
    source.replaceChildren();
    importBtn.disabled = busy || !it;
    if (!it) return;
    const url = it.rights.source_url;
    if (url.startsWith("https://")) {
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "View source and license";
      source.append(link);
    }
    const meta = document.createElement("div");
    meta.textContent = `Credit: ${it.rights?.credit ?? ""} · License: ${it.rights?.license ?? ""}`;
    source.append(meta);
  });

  importBtn.addEventListener("click", async () => {
    const token = list.value;
    if (busy || !token) return;
    setBusy(true);
    setStatus("Adding to the stock shelf…");
    try {
      const res = await fetch("/api/library/stock-catalog/import", { method: "POST", headers: headers(), body: JSON.stringify({ token, folder: context.folder() }) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json() as { id: string };
      await context.available(data.id);
      setStatus("Added to the stock shelf.");
    } catch (e) {
      setStatus(`Import error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  });

  disclosure.addEventListener("toggle", async () => {
    if (!disclosure.open || busy || category.options.length) return;
    await fetchFilters();
    if (kind.value === "audio") await search(true);
  });
}
