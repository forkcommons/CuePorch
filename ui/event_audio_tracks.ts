export interface HlsAudioSource {
  audioTracks: { name: string; lang?: string }[];
  audioTrack: number;
  on(event: string, listener: () => void): void;
  off(event: string, listener: () => void): void;
}
interface NativeAudioTrack {
  enabled: boolean;
  label: string;
  language: string;
}
interface NativeAudioTrackList extends EventTarget {
  readonly length: number;
  readonly [index: number]: NativeAudioTrack;
}
const same = (select: HTMLSelectElement, labels: string[]): boolean => {
  if (select.options.length !== labels.length) return false;
  for (let i = 0; i < labels.length; i++) if (select.options[i].text !== labels[i]) return false;
  return true;
};
const setOptions = (select: HTMLSelectElement, labels: string[]): void => {
  if (same(select, labels)) return;
  select.replaceChildren();
  for (let i = 0; i < labels.length; i++) {
    const option = document.createElement("option");
    option.value = String(i);
    option.textContent = labels[i];
    select.appendChild(option);
  }
};
export function bindEventAudio(
  label: HTMLLabelElement,
  select: HTMLSelectElement,
  source: HlsAudioSource | HTMLVideoElement,
): () => void {
  let disposed = false;
  const cleanups: (() => void)[] = [];
  const cleanup = () => {
    disposed = true;
    for (const fn of cleanups) fn();
    cleanups.length = 0;
    label.hidden = true;
    select.replaceChildren();
  };
  const visibility = (count: number) => {
    label.hidden = count < 2;
  };
  const syncSelect = (index: number) => {
    if (select.options.length === 0) return;
    const clamped = index >= 0 && index < select.options.length ? index : 0;
    if (select.selectedIndex !== clamped) select.selectedIndex = clamped;
  };
  if (typeof HTMLVideoElement !== "undefined" && source instanceof HTMLVideoElement) {
    const tracks = (source as unknown as { audioTracks?: NativeAudioTrackList }).audioTracks;
    if (tracks) {
      const getLabels = (): string[] =>
        Array.from({ length: tracks.length }, (_, i) => {
          const t = tracks[i];
          return t.label || t.language || `Audio ${i + 1}`;
        });
      const sync = (rebuild: boolean) => {
        const labels = getLabels();
        if (rebuild) setOptions(select, labels);
        visibility(labels.length);
        let selected = 0;
        for (let i = 0; i < tracks.length; i++)
          if (tracks[i].enabled) {
            selected = i;
            break;
          }
        syncSelect(selected);
      };
      const onTracks = () => {
        if (!disposed) sync(true);
      };
      const onChange = () => {
        if (!disposed) sync(false);
      };
      tracks.addEventListener("addtrack", onTracks);
      tracks.addEventListener("removetrack", onTracks);
      tracks.addEventListener("change", onChange);
      cleanups.push(() => {
        tracks.removeEventListener("addtrack", onTracks);
        tracks.removeEventListener("removetrack", onTracks);
        tracks.removeEventListener("change", onChange);
      });
      select.onchange = () => {
        const index = select.selectedIndex;
        for (let i = 0; i < tracks.length; i++) tracks[i].enabled = i === index;
      };
      cleanups.push(() => {
        select.onchange = null;
      });
      sync(true);
    } else {
      visibility(0);
    }
    return cleanup;
  }
  const hls = source as HlsAudioSource;
  const getLabels = (): string[] => hls.audioTracks.map((t) => t.name);
  const sync = (rebuild: boolean) => {
    const labels = getLabels();
    if (rebuild) setOptions(select, labels);
    visibility(labels.length);
    syncSelect(hls.audioTrack);
  };
  const onUpdated = () => {
    if (!disposed) sync(true);
  };
  const onSwitched = () => {
    if (!disposed) sync(false);
  };
  hls.on("hlsAudioTracksUpdated", onUpdated);
  hls.on("hlsAudioTrackSwitched", onSwitched);
  cleanups.push(() => {
    hls.off("hlsAudioTracksUpdated", onUpdated);
    hls.off("hlsAudioTrackSwitched", onSwitched);
  });
  select.onchange = () => {
    hls.audioTrack = select.selectedIndex;
  };
  cleanups.push(() => {
    select.onchange = null;
  });
  sync(true);
  return cleanup;
}
