import { eventObservation } from "./event_playback.js";
export function measurePeerEventPlayback(frame, token) {
    if (!token)
        return undefined;
    const origin = new URL(frame.src).origin;
    const report = eventObservation(token);
    const listeners = new AbortController();
    const { signal } = listeners;
    let previous;
    let loaded = false;
    let played = false;
    let playing = false;
    let completed = false;
    let watched = 0;
    let sent = 0;
    function send() {
        sent = performance.now();
        report(played, playing, completed, watched);
    }
    function stop() {
        previous = undefined;
        if (playing) {
            playing = false;
            send();
        }
    }
    // PeerTube uses JSChannel to deliver the Embed API's notifications.
    function handshake(type) {
        frame.contentWindow?.postMessage(JSON.stringify({
            method: "peertube::__ready", params: { type, publish: [] },
        }), origin);
    }
    window.addEventListener("message", event => {
        if (event.origin !== origin || event.source !== frame.contentWindow ||
            typeof event.data !== "string" || event.data.length > 8192)
            return;
        let message;
        try {
            message = JSON.parse(event.data);
        }
        catch {
            return;
        }
        if (!message)
            return;
        const { method, params } = message;
        if (method === "peertube::__ready") {
            if (params?.type === "publish-request")
                handshake("publish-reply");
            return;
        }
        if (method !== "peertube::playbackStatusUpdate")
            return;
        const update = params;
        const position = update?.position;
        const state = update?.playbackState;
        if (typeof position !== "number" || !Number.isFinite(position) || position < 0 ||
            (state !== "playing" && state !== "paused" && state !== "ended" && state !== "unstarted"))
            return;
        const now = performance.now();
        // Count time only when playback advances plausibly between consecutive updates.
        const delta = previous ? position - previous.position : 0;
        const advanced = !!previous && previous.state === "playing" &&
            delta > 0 && delta <= (now - previous.at) / 500 + .2;
        previous = { position, state, at: now };
        if (advanced) {
            played = true;
            watched += delta;
        }
        const finished = !completed && played && state === "ended";
        completed ||= finished;
        const active = state === "playing" && (playing || advanced);
        if (!loaded || active !== playing || finished || (advanced && now - sent >= 10000)) {
            playing = active;
            loaded = true;
            send();
        }
    }, { signal });
    frame.addEventListener("load", () => handshake("publish-request"), { signal });
    window.addEventListener("pagehide", stop, { signal });
    handshake("publish-request");
    return () => { stop(); listeners.abort(); };
}
