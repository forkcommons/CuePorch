interface CaptionCue {starts: number; ends: number; text: string;}

export function setupLiveCaptions(grant: string, embed: boolean) {
  const region = document.querySelector<HTMLElement>("#eventCaptions")!;
  const list = region.querySelector<HTMLOListElement>("ol")!;
  const toggle = region.querySelector<HTMLButtonElement>("#toggleLiveCaptions")!;
  const status = region.querySelector<HTMLElement>("[role=status]")!;
  const cues = new Map<number, CaptionCue>();
  const added = new Set<number>();
  let endpoint = region.dataset.endpoint || "";
  let source: HTMLVideoElement | undefined;
  let track: TextTrack | undefined;
  let clock: (() => number | null) | undefined;
  let reading = false;
  let isEnabled = true;
  let shown = false;
  let since = 0;

  function timestamp() {
    const mediaDate = clock?.();
    return mediaDate != null && Number.isFinite(mediaDate) && source ? mediaDate - source.currentTime * 1000 : null;
  }

  function install() {
    const zero = timestamp();
    if (!track || zero === null) return;
    for (const cue of cues.values()) {
      if (added.has(cue.starts) || cue.ends <= zero) continue;
      const begin = Math.max(0, (cue.starts - zero) / 1000);
      track.addCue(new VTTCue(begin, (cue.ends - zero) / 1000, cue.text));
      added.add(cue.starts);
    }
  }

  function clear() {
    if (track && track.cues) for (const cue of Array.from(track.cues)) track.removeCue(cue);
    cues.clear();
    added.clear();
    list.replaceChildren();
    since = 0;
  }

  function bind(video?: HTMLVideoElement, date?: () => number | null) {
    source?.removeEventListener("timeupdate", install);
    if (track) track.mode = "disabled";
    source = video;
    clock = date;
    track = video && endpoint ? video.addTextTrack("captions", "Live captions") : undefined;
    if (track) track.mode = shown ? "showing" : "hidden";
    added.clear();
    video?.addEventListener("timeupdate", install);
    install();
  }

  async function poll() {
    if (!endpoint || reading) return;
    reading = true;
    const active = endpoint;
    try {
      const response = await fetch(`${active}?since=${since}`, {cache: "no-store", headers: {
        "X-CuePorch-Event-Viewer": grant, "X-CuePorch-Chat-Embed": embed ? "yes" : "no",
      }});
      if (active !== endpoint) return;
      if (response.status === 404) { select(""); return; }
      if (!response.ok) return;
      const received: {enabled: boolean; cues: CaptionCue[]} = await response.json();
      isEnabled = received.enabled;
      region.hidden = !isEnabled;
      if (!isEnabled) { clear(); return; }
      for (const cue of received.cues) {
        if (cues.has(cue.starts)) continue;
        cues.set(cue.starts, cue);
        since = Math.max(since, cue.starts);
        const row = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = cue.text;
        button.onclick = () => {
          const zero = timestamp();
          if (!source || zero === null) return;
          const position = (cue.starts - zero) / 1000;
          for (let i = 0; i < source.seekable.length; i++) {
            if (position >= source.seekable.start(i) && position < source.seekable.end(i)) {
              // Seek just inside the cue so rounded media timestamps activate it.
              const available = Math.min((cue.ends - zero) / 1000, source.seekable.end(i)) - position;
              source.currentTime = position + Math.min(0.08, available / 2);
              return;
            }
          }
          status.textContent = "This line is outside the available replay window.";
        };
        row.append(button);
        list.append(row);
        if (list.children.length > 200) list.firstElementChild?.remove();
      }
      toggle.disabled = cues.size === 0;
      status.textContent = cues.size ? "" : "Waiting for live captions.";
      install();
    } catch {
      // Keep the last received cues while the connected service is briefly unavailable.
    } finally { reading = false; }
  }

  function select(path: string) {
    if (endpoint !== path) clear();
    endpoint = path;
    if (!path) { region.hidden = true; return; }
    if (!track && source) {
      track = source.addTextTrack("captions", "Live captions");
      track.mode = shown ? "showing" : "hidden";
    }
    void poll();
  }

  toggle.onclick = () => {
    shown = !shown;
    toggle.setAttribute("aria-pressed", String(shown));
    if (track) track.mode = shown ? "showing" : "hidden";
    toggle.textContent = shown ? "Hide live captions" : "Show live captions";
  };

  const timer = window.setInterval(() => { if (!document.hidden) void poll(); }, 2500);
  window.addEventListener("pagehide", () => window.clearInterval(timer), {once: true});
  void poll();
  return {bind, select};
}
