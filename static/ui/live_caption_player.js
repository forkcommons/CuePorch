export function setupLiveCaptions(grant, embed, access) {
    const region = document.querySelector("#eventCaptions");
    const list = region.querySelector("ol");
    const toggle = region.querySelector("#toggleLiveCaptions");
    const transcript = region.querySelector("#eventTranscriptLabel");
    const status = region.querySelector("[role=status]");
    const cues = new Map();
    const added = new Set();
    let endpoint = region.dataset.endpoint || "";
    let source;
    let track;
    let clock;
    let reading = false;
    let isEnabled = true;
    let shown = false;
    let replay = false;
    let revision = "";
    let since = -1;
    function label() { return replay ? "Replay captions" : "Live captions"; }
    function updateLabel() {
        transcript.textContent = replay ? "Replay transcript" : "Live transcript";
        region.setAttribute("aria-label", label());
        toggle.textContent = (shown ? "Hide " : "Show ") + label().toLowerCase();
    }
    function timestamp() {
        const mediaDate = clock?.();
        return mediaDate != null && Number.isFinite(mediaDate) && source ? mediaDate - source.currentTime * 1000 : null;
    }
    function install() {
        const zero = timestamp();
        if (!track || zero === null)
            return;
        for (const cue of cues.values()) {
            if (added.has(cue.starts) || cue.ends <= zero)
                continue;
            const begin = Math.max(0, (cue.starts - zero) / 1000);
            track.addCue(new VTTCue(begin, (cue.ends - zero) / 1000, cue.text));
            added.add(cue.starts);
        }
    }
    function clear() {
        if (track && track.cues)
            for (const cue of Array.from(track.cues))
                track.removeCue(cue);
        cues.clear();
        added.clear();
        list.replaceChildren();
        since = -1;
        revision = "";
    }
    function bind(video, date, archived = false) {
        if (replay !== archived)
            clear();
        replay = archived;
        updateLabel();
        source?.removeEventListener("timeupdate", install);
        if (track)
            track.mode = "disabled";
        source = video;
        clock = date;
        track = video && endpoint ? video.addTextTrack("captions", label()) : undefined;
        if (track)
            track.mode = shown ? "showing" : "hidden";
        added.clear();
        video?.addEventListener("timeupdate", install);
        install();
    }
    async function poll() {
        if (!endpoint || reading)
            return;
        reading = true;
        const active = endpoint;
        try {
            const address = new URL(active, location.href);
            address.searchParams.set("since", String(since));
            if (revision)
                address.searchParams.set("revision", revision);
            const response = await fetch(address, { cache: "no-store", headers: {
                    "X-CuePorch-Event-Viewer": grant, "X-CuePorch-Chat-Embed": embed ? "yes" : "no",
                    "X-CuePorch-Event-Access": access,
                } });
            if (active !== endpoint)
                return;
            if (response.status === 404) {
                select("");
                return;
            }
            if (!response.ok)
                return;
            const received = await response.json();
            if (active !== endpoint)
                return;
            isEnabled = received.enabled;
            region.hidden = !isEnabled;
            if (!isEnabled) {
                clear();
                return;
            }
            if (received.revision && revision !== received.revision) {
                clear();
                revision = received.revision;
            }
            for (const cue of received.cues) {
                if (cues.has(cue.starts))
                    continue;
                cues.set(cue.starts, cue);
                since = Math.max(since, cue.starts);
                const row = document.createElement("li");
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = cue.text;
                button.onclick = () => {
                    const zero = timestamp();
                    if (!source || zero === null)
                        return;
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
                if (list.children.length > 200)
                    list.firstElementChild?.remove();
            }
            toggle.disabled = cues.size === 0;
            status.textContent = cues.size ? "" : `Waiting for ${label().toLowerCase()}.`;
            install();
        }
        catch {
            // Keep the last received cues while the connected service is briefly unavailable.
        }
        finally {
            reading = false;
        }
    }
    function select(path) {
        if (endpoint !== path)
            clear();
        endpoint = path;
        if (!path) {
            region.hidden = true;
            return;
        }
        if (!track && source) {
            track = source.addTextTrack("captions", label());
            track.mode = shown ? "showing" : "hidden";
        }
        void poll();
    }
    toggle.onclick = () => {
        shown = !shown;
        toggle.setAttribute("aria-pressed", String(shown));
        if (track)
            track.mode = shown ? "showing" : "hidden";
        updateLabel();
    };
    const timer = window.setInterval(() => { if (!document.hidden)
        void poll(); }, 2500);
    window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
    void poll();
    return { bind, select };
}
