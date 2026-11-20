export {};

const panel = document.querySelector<HTMLElement>("[data-archive-audio]")!;
const choice = panel.querySelector<HTMLSelectElement>("select")!;
const notice = panel.querySelector<HTMLElement>("[data-audio-status]")!;
const video = document.querySelector<HTMLVideoElement>("video")!;
const download = document.querySelector<HTMLAnchorElement>("[data-audio-download]");
const original = new URL(video.src);
original.hash = "";
let selected = choice.options[0];

function source(option: HTMLOptionElement) {
  return option.dataset.src || original.href;
}

function ready(event: "loadedmetadata" | "seeked", change: () => void) {
  return new Promise<void>((resolve, reject) => {
    const listeners = new AbortController();
    const wait = window.setTimeout(() => fail(), 15_000);
    const cleanup = () => { window.clearTimeout(wait); listeners.abort(); };
    const fail = () => { cleanup(); reject(new Error("Audio playback could not load.")); };
    video.addEventListener(event, () => { cleanup(); resolve(); }, {signal: listeners.signal, once: true});
    video.addEventListener("error", fail, {signal: listeners.signal, once: true});
    try { change(); } catch { fail(); }
  });
}

choice.onchange = async () => {
  const next = choice.selectedOptions[0];
  if (!next || next === selected) return;
  const position = video.currentTime;
  const rate = video.playbackRate;
  const playing = !video.paused && !video.ended;
  const modes = Array.from(video.textTracks, track => track.mode);
  choice.disabled = true;
  video.autoplay = false;
  notice.textContent = "Loading audio…";
  video.dispatchEvent(new Event("cueporch-audio-before-change"));
  video.pause();

  async function load(option: HTMLOptionElement) {
    await ready("loadedmetadata", () => { video.src = source(option); video.load(); });
    video.playbackRate = rate;
    for (const [index, track] of Array.from(video.textTracks).entries()) {
      if (modes[index]) track.mode = modes[index];
    }
    const destination = Math.min(position, video.duration);
    if (Number.isFinite(destination) && destination > 0.01) {
      await ready("seeked", () => { video.currentTime = destination; });
    }
    selected = option;
    video.dataset.audioTrack = option.value;
    video.dataset.analyticsAudioTrack = option.dataset.analyticsTrack || "original";
    if (download) {
      const path = new URL(source(option), window.location.href);
      path.searchParams.set("download", "1");
      download.href = path.href;
    }
  }

  try {
    await load(next);
    notice.textContent = "";
  } catch {
    try {
      await load(selected);
      notice.textContent = "Could not switch audio. The previous track was restored.";
    } catch {
      notice.textContent = "Audio could not load. Reopen the video to try again.";
    }
  } finally {
    video.dispatchEvent(new Event("cueporch-audio-change"));
    choice.value = selected.value;
    choice.disabled = false;
    if (playing && video.readyState >= HTMLMediaElement.HAVE_METADATA) {
      await video.play().catch(() => { notice.textContent ||= "Select Play to resume."; });
    }
  }
};
panel.hidden = false;
