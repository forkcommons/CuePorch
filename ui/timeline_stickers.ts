export interface TimelineSticker {
  work: string;
  cut: string;
  position: string;
  size: number;
}

export interface StickerItem {
  id: string;
  title: string;
  asset_type?: string;
  source_cut?: string | null;
}

export function mountTimelineStickers(save: (sticker: TimelineSticker | null) => void): {
  show(sticker: TimelineSticker | null, hasClip: boolean, items: StickerItem[]): void;
} {
  const assetSelect = document.getElementById('timelineStickerAsset') as HTMLSelectElement | null;
  const positionSelect = document.getElementById('timelineStickerPosition') as HTMLSelectElement | null;
  const sizeInput = document.getElementById('timelineStickerSize') as HTMLInputElement | null;
  const applyButton = document.getElementById('applyTimelineSticker') as HTMLButtonElement | null;
  const clearButton = document.getElementById('clearTimelineSticker') as HTMLButtonElement | null;

  if (!assetSelect || !positionSelect || !sizeInput || !applyButton || !clearButton) {
    throw new Error('timeline sticker controls missing');
  }

  let currentSticker: TimelineSticker | null = null;
  let lastHasClip = false;
  let itemsById = new Map<string, StickerItem>();

  const clampSize = (value: number): number => {
    if (!Number.isSafeInteger(value)) return 30;
    return Math.min(60, Math.max(10, value));
  };

  assetSelect.addEventListener('change', () => {
    applyButton.disabled = !lastHasClip || assetSelect.value === '';
  });

  applyButton.addEventListener('click', () => {
    if (!lastHasClip) return;

    const item = itemsById.get(assetSelect.value);
    if (!item || typeof item.source_cut !== 'string' || item.source_cut.length === 0) return;

    const cut = currentSticker && currentSticker.work === item.id && currentSticker.cut
      ? currentSticker.cut
      : item.source_cut;

    const position = positionSelect.value || 'bottom-right';
    const parsed = Number.parseInt(sizeInput.value, 10);
    const size = clampSize(parsed);

    save({ work: item.id, cut, position, size });
  });

  clearButton.addEventListener('click', () => {
    if (lastHasClip) save(null);
  });

  return {
    show(sticker, hasClip, items) {
      currentSticker = sticker;
      lastHasClip = hasClip;
      itemsById = new Map<string, StickerItem>();

      assetSelect.innerHTML = '';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Select asset';
      assetSelect.appendChild(placeholder);

      for (const item of items) {
        if (item.asset_type !== 'image') continue;
        if (typeof item.source_cut !== 'string' || item.source_cut.length === 0) continue;

        itemsById.set(item.id, item);
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.title;
        assetSelect.appendChild(option);
      }

      assetSelect.disabled = !hasClip;
      positionSelect.disabled = !hasClip;
      sizeInput.disabled = !hasClip;
      applyButton.disabled = !hasClip;
      clearButton.disabled = sticker == null;

      if (sticker && itemsById.has(sticker.work)) {
        assetSelect.value = sticker.work;
      } else {
        assetSelect.value = '';
      }

      if (sticker && Array.from(positionSelect.options).some((option) => option.value === sticker.position)) {
        positionSelect.value = sticker.position;
      } else {
        positionSelect.value = 'bottom-right';
      }

      sizeInput.value = String(clampSize(sticker ? sticker.size : 30));
      applyButton.disabled = !hasClip || assetSelect.value === '';
    }
  };
}
