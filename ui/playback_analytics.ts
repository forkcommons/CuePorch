import {browserIdentity} from "./browser_analytics.js";

const video = document.querySelector<HTMLVideoElement>("video[data-analytics]")!;
const viewer = browserIdentity();
let sequence = 0;
let played = !video.paused || video.played.length > 0;
let completed = video.ended;
let watched = 0;
let position = video.currentTime;
let last = performance.now();
let lastSent = 0;
const captionTime = new Map<string, number>();
const textTracks = Array.from(video.querySelectorAll("track"));

function sample() {
  const now = performance.now();
  const delta = video.currentTime - position;
  const expected = (now - last) / 1000 * Math.abs(video.playbackRate) + .25;
  if (played && !video.seeking && delta > 0 && delta <= expected) {
    watched += delta;
    const visible = textTracks.find(track => track.track.mode === "showing");
    const key = visible ? visible.readyState === HTMLTrackElement.LOADED ? visible.dataset.analyticsTrack : undefined : "none";
    if (key) captionTime.set(key, (captionTime.get(key) ?? 0) + delta);
  }
  position = video.currentTime;
  last = now;
}

function send() {
  const playedIntervals = video.played;
  const intervals = [];
  let edge = 0;
  for (let i = 0; i < Math.min(playedIntervals.length, 512); i++) {
    const start = Math.max(edge, Math.round(playedIntervals.start(i) * 1000));
    const end = Math.round(playedIntervals.end(i) * 1000);
    if (end > start) { intervals.push([start, end]); edge = end; }
  }
  lastSent = performance.now();
  void fetch(video.dataset.analytics!, {
    method: "POST",
    keepalive: true,
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({sequence: ++sequence, played, completed, watched: Math.round(watched * 1000), intervals, viewer,
      captions: [...captionTime].slice(0, 256).map(([key, seconds]) => [key, Math.round(seconds * 1000)])}),
  }).catch(() => {});
}

video.addEventListener("playing", () => {
  position = video.currentTime;
  last = performance.now();
  if (!played) { played = true; send(); }
});
video.addEventListener("timeupdate", () => {
  sample();
  if (played && performance.now() - lastSent >= 5000) send();
});
video.addEventListener("seeking", () => { position = video.currentTime; last = performance.now(); });
video.addEventListener("pause", () => { sample(); if (played) send(); });
video.addEventListener("ended", () => { completed = true; sample(); send(); });
window.addEventListener("pagehide", () => { sample(); if (played) send(); });
send();
