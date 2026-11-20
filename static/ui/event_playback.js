import { browserIdentity } from "./browser_analytics.js";
export function measureEventPlayback(video, token) {
    if (!token)
        return undefined;
    const viewer = browserIdentity();
    const listeners = new AbortController();
    const { signal } = listeners;
    let sequence = 0;
    let played = false;
    let playing = false;
    let completed = false;
    let watched = 0;
    let position = video.currentTime;
    let sampleTime = performance.now();
    let sent = sampleTime;
    let interrupted = false;
    function sample() {
        const now = performance.now();
        const delta = video.currentTime - position;
        const elapsed = (now - sampleTime) / 1000;
        const plausible = elapsed * Math.abs(video.playbackRate) + .25;
        const advanced = playing && !interrupted && !video.seeking && delta > 0 && delta <= plausible;
        if (advanced)
            watched += delta;
        position = video.currentTime;
        sampleTime = now;
        return advanced;
    }
    function send() {
        sent = performance.now();
        void fetch("/api/event-playback", {
            method: "POST", keepalive: true,
            headers: { "Content-Type": "application/json", "X-CuePorch-Playback": token },
            body: JSON.stringify({ sequence: ++sequence, played, playing, completed, watched: Math.round(watched * 1000), viewer }),
        }).catch(() => { });
    }
    function start() {
        sample();
        interrupted = false;
        if (!playing) {
            played = playing = true;
            send();
        }
    }
    function stop() {
        sample();
        if (playing) {
            playing = false;
            send();
        }
    }
    video.addEventListener("playing", start, { signal });
    video.addEventListener("timeupdate", () => {
        if (sample() && performance.now() - sent >= 10000)
            send();
    }, { signal });
    // Keep brief stalls in the audience window, but send no heartbeat without new playback.
    for (const event of ["waiting", "seeking"]) {
        video.addEventListener(event, () => { sample(); interrupted = true; }, { signal });
    }
    for (const event of ["pause", "error"])
        video.addEventListener(event, stop, { signal });
    video.addEventListener("ended", () => {
        completed = played;
        if (playing)
            stop();
        else if (completed)
            send();
    }, { signal });
    video.addEventListener("seeked", () => {
        if (!video.paused && video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA)
            start();
    }, { signal });
    window.addEventListener("pagehide", stop, { signal });
    window.addEventListener("pageshow", () => {
        if (!video.paused && video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA)
            start();
    }, { signal });
    send();
    return () => { stop(); listeners.abort(); };
}
