import { browserIdentity } from "./browser_analytics.js";
const video = document.querySelector("video[data-analytics]");
const viewer = browserIdentity();
let sequence = 0;
let played = !video.paused || video.played.length > 0;
let completed = video.ended;
let watched = 0;
let position = video.currentTime;
let last = performance.now();
let lastSent = 0;
let switching = false;
const captionTime = new Map();
const trackTime = new Map();
const textTracks = Array.from(video.querySelectorAll("track"));
let audio = audioKey();
let caption = captionKey();
let previousIntervals = [];
function audioKey() {
    return video.dataset.analyticsAudioTrack || "original";
}
function captionKey() {
    const visible = textTracks.find(track => track.track.mode === "showing");
    if (!visible)
        return "none";
    return visible.readyState === HTMLTrackElement.LOADED ? visible.dataset.analyticsTrack || "unknown" : "unknown";
}
function sample() {
    const now = performance.now();
    const delta = video.currentTime - position;
    const expected = (now - last) / 1000 * Math.abs(video.playbackRate) + .25;
    if (played && !switching && !video.seeking && delta > 0 && delta <= expected) {
        watched += delta;
        if (caption !== "unknown")
            captionTime.set(caption, (captionTime.get(caption) ?? 0) + delta);
        const key = `${audio}|${caption}`;
        const pair = trackTime.get(key);
        if (pair)
            pair.seconds += delta;
        else
            trackTime.set(key, { audio, caption, seconds: delta });
    }
    position = video.currentTime;
    last = now;
    audio = audioKey();
    caption = captionKey();
}
function coveredIntervals() {
    const playedIntervals = video.played;
    const fragments = [...previousIntervals];
    for (let i = 0; i < Math.min(playedIntervals.length, 512); i++) {
        const start = Math.round(playedIntervals.start(i) * 1000);
        const end = Math.round(playedIntervals.end(i) * 1000);
        if (end > start)
            fragments.push([start, end]);
    }
    const intervals = [];
    for (const [first, last] of fragments.sort(([a], [b]) => a - b)) {
        const prior = intervals.at(-1);
        if (prior && first <= prior[1])
            prior[1] = Math.max(prior[1], last);
        else
            intervals.push([first, last]);
    }
    return intervals.slice(0, 512);
}
function send() {
    const intervals = coveredIntervals();
    const body = JSON.stringify({ sequence: ++sequence, played, completed, watched: Math.round(watched * 1000), intervals, viewer,
        captions: [...captionTime].slice(0, 256).map(([key, seconds]) => [key, Math.round(seconds * 1000)]),
        tracks: [...trackTime.values()].slice(0, 256).map(({ audio, caption, seconds }) => [audio, caption, Math.round(seconds * 1000)]) });
    lastSent = performance.now();
    void fetch(video.dataset.analytics, {
        method: "POST",
        keepalive: body.length < 60_000,
        headers: { "Content-Type": "application/json" },
        body,
    }).catch(() => { });
}
video.addEventListener("playing", () => {
    position = video.currentTime;
    last = performance.now();
    audio = audioKey();
    caption = captionKey();
    if (!played) {
        played = true;
        send();
    }
});
video.addEventListener("cueporch-audio-before-change", () => {
    sample();
    previousIntervals = coveredIntervals();
    switching = true;
});
video.addEventListener("cueporch-audio-change", () => {
    sample();
    switching = false;
});
video.textTracks.addEventListener("change", sample);
for (const track of textTracks)
    track.addEventListener("load", sample);
video.addEventListener("timeupdate", () => {
    sample();
    if (played && performance.now() - lastSent >= 5000)
        send();
});
video.addEventListener("seeking", () => { position = video.currentTime; last = performance.now(); });
video.addEventListener("pause", () => { sample(); if (played)
    send(); });
video.addEventListener("ended", () => { completed = true; sample(); send(); });
window.addEventListener("pagehide", () => { sample(); if (played)
    send(); });
send();
