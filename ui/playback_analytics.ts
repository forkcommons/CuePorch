import {browserIdentity} from "./browser_analytics.js";

// Keep observations for one media item across playlist changes.
export function playbackObservation(endpoint: string, anonymous = false, hubspot = false) {
  const viewer = anonymous ? undefined : browserIdentity();
  let sequence = 0;
  let played = false;
  let completed = false;
  let watched = 0;
  const captionTime = new Map<string, number>();
  const trackTime = new Map<string, {audio: string; caption: string; seconds: number}>();
  let previousIntervals: [number, number][] = [];

  return (video: HTMLVideoElement, resume = false) => {
    const controller = new AbortController();
    const listeners = {signal: controller.signal};
    let started = resume && (!video.paused || (!hubspot && video.played.length > 0));
    played ||= started;
    completed ||= resume && !hubspot && video.ended;
    let position = video.currentTime;
    let last = performance.now();
    let lastSent = 0;
    let switching = false;
    const freshIntervals: [number, number][] = [];
    const textTracks = Array.from(video.querySelectorAll("track"));
    let audio = audioKey();
    let caption = captionKey();

    function audioKey() {
      return video.dataset.analyticsAudioTrack || "original";
    }

    function captionKey() {
      const visible = textTracks.find(track => track.track.mode === "showing");
      if (!visible) return "none";
      return visible.readyState === HTMLTrackElement.LOADED ? visible.dataset.analyticsTrack || "unknown" : "unknown";
    }

    function sample() {
      const now = performance.now();
      const delta = video.currentTime - position;
      const expected = (now - last) / 1000 * Math.abs(video.playbackRate) + .25;
      if (started && !switching && !video.seeking && delta > 0 && delta <= expected) {
        watched += delta;
        if (hubspot) {
          const begin = Math.round(position * 1000);
          const end = Math.round(video.currentTime * 1000);
          if (end > begin) {
            const previous = freshIntervals[freshIntervals.length - 1];
            if (previous && begin >= previous[0] && begin <= previous[1]) {
              previous[1] = Math.max(previous[1], end);
            } else if (freshIntervals.length < 512) {
              freshIntervals.push([begin, end]);
            }
          }
        }
        if (caption !== "unknown") captionTime.set(caption, (captionTime.get(caption) ?? 0) + delta);
        const key = `${audio}|${caption}`;
        const pair = trackTime.get(key);
        if (pair) pair.seconds += delta;
        else trackTime.set(key, {audio, caption, seconds: delta});
      }
      position = video.currentTime;
      last = now;
      audio = audioKey();
      caption = captionKey();
    }

    function coveredIntervals() {
      const playedIntervals = video.played;
      const fragments = [...previousIntervals, ...freshIntervals];
      for (let i = 0; !hubspot && started && i < Math.min(playedIntervals.length, 512); i++) {
        const start = Math.round(playedIntervals.start(i) * 1000);
        const end = Math.round(playedIntervals.end(i) * 1000);
        if (end > start) fragments.push([start, end]);
      }
      const intervals: [number, number][] = [];
      for (const [first, last] of fragments.sort(([a], [b]) => a - b)) {
        const prior = intervals.at(-1);
        if (prior && first <= prior[1]) prior[1] = Math.max(prior[1], last);
        else intervals.push([first, last]);
      }
      return intervals.slice(0, 512);
    }

    function send() {
      const intervals = coveredIntervals();
      const body = JSON.stringify({sequence: ++sequence, played, completed, watched: Math.round(watched * 1000), intervals, viewer,
        captions: [...captionTime].slice(0, 256).map(([key, seconds]) => [key, Math.round(seconds * 1000)]),
        tracks: [...trackTime.values()].slice(0, 256).map(({audio, caption, seconds}) => [audio, caption, Math.round(seconds * 1000)])});
      lastSent = performance.now();
      void fetch(endpoint, {
        method: "POST",
        keepalive: body.length < 60_000,
        headers: {"Content-Type": "application/json", ...(hubspot ? {"X-CuePorch-HubSpot-View": "1"} : {})},
        body,
      }).catch(() => {});
    }

    video.addEventListener("playing", () => {
      started = true;
      position = video.currentTime;
      last = performance.now();
      audio = audioKey();
      caption = captionKey();
      if (!played) { played = true; send(); }
    }, listeners);
    video.addEventListener("cueporch-audio-before-change", () => {
      sample();
      previousIntervals = coveredIntervals();
      switching = true;
    }, listeners);
    video.addEventListener("cueporch-audio-change", () => {
      sample();
      switching = false;
    }, listeners);
    video.textTracks.addEventListener("change", sample, listeners);
    for (const track of textTracks) track.addEventListener("load", sample, listeners);
    video.addEventListener("timeupdate", () => {
      sample();
      if (started && performance.now() - lastSent >= 5000) send();
    }, listeners);
    video.addEventListener("seeking", () => { position = video.currentTime; last = performance.now(); }, listeners);
    video.addEventListener("pause", () => { sample(); if (started) send(); }, listeners);
    video.addEventListener("ended", () => { completed = true; sample(); send(); }, listeners);
    window.addEventListener("pagehide", () => { sample(); if (started) send(); }, listeners);
    send();

    return (report = true) => {
      if (started && report) {
        sample();
        previousIntervals = coveredIntervals();
        send();
      }
      controller.abort();
    };
  };
}

const standalone = document.querySelector<HTMLVideoElement>("video[data-analytics]");
if (standalone) playbackObservation(standalone.dataset.analytics!, standalone.hasAttribute("data-team-view"))(standalone, true);
const withdraw=document.querySelector<HTMLButtonElement>("button[data-salesforce-withdraw]"), status=document.querySelector<HTMLElement>("[data-salesforce-withdraw-status]"), url=withdraw?.dataset.salesforceWithdraw;
if (withdraw && status && url) {
  withdraw.addEventListener("click", async () => {
    withdraw.disabled = true;
    try {
      const response = await fetch(url, {
        method: "DELETE",
        headers: {
          "Content-Type": "application/json",
          "X-CuePorch-Salesforce-View": "1",
        },
        body: "{}",
      });
      if (!response.ok) throw new Error("Salesforce video reporting stop failed");
      withdraw.hidden = true;
      status.textContent = "Salesforce video reporting stopped.";
    } catch {
      withdraw.disabled = false;
      status.textContent = "Could not stop Salesforce video reporting. Try again.";
    }
  });
}
