export interface TimelineGraphic {
  id: string;
  position: string;
  color: string;
  text: string;
}

export interface GraphicAsset {
  id: string;
  title: string;
  category: string;
  text: boolean;
}

export function mountTimelineGraphics(
  assets: GraphicAsset[],
  receive: (value: TimelineGraphic | null) => void,
): { show: (graphic: TimelineGraphic | null, enabled: boolean) => void } {
  const get = <T extends HTMLElement>(selector: string): T => {
    const el = document.querySelector<T>(selector);
    if (!el) throw new Error(`Missing element: ${selector}`);
    return el;
  };

  const catalog = get<HTMLSelectElement>('#timelineGraphic');
  const search = get<HTMLInputElement>('#timelineGraphicSearch');
  const category = get<HTMLSelectElement>('#timelineGraphicCategory');
  const position = get<HTMLSelectElement>('#timelineGraphicPosition');
  const color = get<HTMLInputElement>('#timelineGraphicColor');
  const text = get<HTMLInputElement | HTMLTextAreaElement>('#timelineGraphicText');
  const apply = get<HTMLButtonElement>('#applyTimelineGraphic');
  const clear = get<HTMLButtonElement>('#clearTimelineGraphic');

  const assetById = new Map(assets.map((asset) => [asset.id, asset]));
  const categories = Array.from(new Set(assets.map((asset) => asset.category))).filter(Boolean);

  category.replaceChildren(new Option('All categories', ''));
  for (const name of categories) category.add(new Option(name, name));

  let selectedId = '';
  let enabled = false;
  let savedKey: string | null = null;
  let hasSavedGraphic = false;

  const matches = (asset: GraphicAsset): boolean => {
    const term = search.value.trim().toLowerCase();
    const wanted = category.value;
    if (wanted && asset.category !== wanted) return false;
    if (!term) return true;
    return asset.title.toLowerCase().includes(term) || asset.category.toLowerCase().includes(term);
  };

  const syncDisabled = (): void => {
    catalog.disabled = !enabled;
    search.disabled = !enabled;
    category.disabled = !enabled;
    position.disabled =
      !enabled || selectedId === 'frame' || selectedId === 'gradient';
    color.disabled = !enabled;
    text.disabled = !enabled || !assetById.get(selectedId)?.text;
    apply.disabled = !enabled || !selectedId;
    clear.disabled = !enabled || !hasSavedGraphic;
  };

  const renderCatalog = (): void => {
    const items = assets.filter(matches);
    catalog.replaceChildren();
    catalog.add(new Option('Choose a graphic…', ''));
    const current = selectedId ? assetById.get(selectedId) : undefined;
    if (current && !items.some((asset) => asset.id === current.id)) {
      catalog.add(new Option(current.title, current.id));
    }
    for (const asset of items) catalog.add(new Option(asset.title, asset.id));
    catalog.value = selectedId;
    syncDisabled();
  };

  const show = (graphic: TimelineGraphic | null, isEnabled: boolean): void => {
    enabled = isEnabled;
    const asset = graphic && isEnabled ? assetById.get(graphic.id) : undefined;
    const key = asset && graphic
      ? `${graphic.id}|${graphic.position ?? ''}|${graphic.color ?? ''}|${graphic.text ?? ''}`
      : '';
    hasSavedGraphic = Boolean(asset);

    if (key && key === savedKey) {
      syncDisabled();
      return;
    }
    savedKey = key;
    selectedId = asset ? asset.id : '';

    if (asset && graphic) {
      const wantedPosition = graphic.position ?? '';
      if (position.value !== wantedPosition) position.value = wantedPosition;

      const hex = (graphic.color ?? '').replace(/^#/, '');
      const wantedColor = /^([0-9a-f]{3}|[0-9a-f]{6})$/i.test(hex) ? `#${hex}` : '#000000';
      if (color.value !== wantedColor) color.value = wantedColor;

      const wantedText = graphic.text ?? '';
      if (text.value !== wantedText) text.value = wantedText;
    } else {
      if (catalog.value) catalog.value = '';
      if (position.value !== 'center') position.value = 'center';
      if (text.value) text.value = '';
      if (color.value !== '#e5a323') color.value = '#e5a323';
    }

    renderCatalog();
  };

  catalog.addEventListener('change', () => {
    selectedId = catalog.value;
    syncDisabled();
  });
  search.addEventListener('input', renderCatalog);
  category.addEventListener('change', renderCatalog);

  apply.addEventListener('click', () => {
    if (apply.disabled || !selectedId) return;
    receive({
      id: selectedId,
      position: position.value,
      color: color.value.replace(/^#/, ''),
      text: text.value,
    });
  });

  clear.addEventListener('click', () => {
    if (clear.disabled) return;
    receive(null);
  });

  renderCatalog();
  syncDisabled();

  return { show };
}

export function createTimelineOverlayPreview<T>(kind: 'graphic' | 'sticker' | 'text'): (spec: T | null, ratio: string, color: string, credential: string) => void {
  const suffix = { graphic: 'Graphic', sticker: 'Sticker', text: 'Text' }[kind];
  const imageId = `timeline${suffix}Preview`;
  const statusId = `timeline${suffix}Status`;
  const canvasClass = `has-timeline-${kind}`;
  const endpoint = `/api/library/timeline-${kind}s/preview`;

  let controller: AbortController | null = null;
  let objectUrl: string | null = null;
  let cacheKey: string | null = null;

  const revokeCurrentUrl = () => {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  };

  const revokeUrl = (url: string) => {
    URL.revokeObjectURL(url);
    if (objectUrl === url) objectUrl = null;
  };

  const clearOverlay = () => {
    const canvas = document.getElementById('timelinePreviewCanvas')!;
    const img = document.getElementById(imageId)! as HTMLImageElement;
    canvas.classList.remove(canvasClass);
    img.onload = null;
    img.onerror = null;
    img.removeAttribute('src');
    img.hidden = true;
  };

  return (spec, ratio, color, credential) => {
    const canvas = document.getElementById('timelinePreviewCanvas')!;
    const img = document.getElementById(imageId)! as HTMLImageElement;
    const status = document.getElementById(statusId)!;

    if (!spec) {
      if (controller) {
        controller.abort();
        controller = null;
      }
      cacheKey = null;
      revokeCurrentUrl();
      clearOverlay();
      return;
    }

    canvas.style.setProperty('--timeline-aspect', ratio.replace(':', ' / '));
    canvas.style.setProperty('--timeline-background', `#${color.replace(/^#/, '')}`);

    const key = `${JSON.stringify(spec)}|${ratio}`;
    if (key === cacheKey) return;

    if (controller) controller.abort();
    revokeCurrentUrl();
    clearOverlay();

    const local = new AbortController();
    controller = local;
    cacheKey = key;

    const run = async () => {
      let createdUrl: string | null = null;
      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${credential}` },
          body: JSON.stringify({ [kind]: spec, ratio }),
          signal: local.signal,
        });
        if (controller !== local) return;
        if (!res.ok) throw new Error(`Preview failed (${res.status})`);
        const blob = await res.blob();
        if (controller !== local) return;
        if (!blob.type.startsWith('image/png')) throw new Error('Preview is not a PNG');

        const url = URL.createObjectURL(blob);
        createdUrl = url;
        objectUrl = url;

        img.onload = () => {
          if (controller !== local) {
            revokeUrl(url);
            return;
          }
          img.hidden = false;
          canvas.classList.add(canvasClass);
          status.textContent = '';
        };
        img.onerror = () => {
          revokeUrl(url);
          if (controller !== local) return;
          img.hidden = true;
          cacheKey = null;
          status.textContent = 'Preview image failed to load';
        };
        img.src = url;
      } catch (err) {
        if (createdUrl) revokeUrl(createdUrl);
        if (local.signal.aborted || controller !== local) return;
        cacheKey = null;
        status.textContent = err instanceof Error ? err.message : 'Preview failed';
      }
    };

    void run();
  };
}

export function createTimelineGraphicPreview(): (graphic: TimelineGraphic | null, ratio: string, color: string, credential: string) => void {
  return createTimelineOverlayPreview<TimelineGraphic>('graphic');
}
