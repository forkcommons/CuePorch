const same = (select, labels) => {
    if (select.options.length !== labels.length)
        return false;
    for (let i = 0; i < labels.length; i++)
        if (select.options[i].text !== labels[i])
            return false;
    return true;
};
const setOptions = (select, labels) => {
    if (same(select, labels))
        return;
    select.replaceChildren();
    for (let i = 0; i < labels.length; i++) {
        const option = document.createElement("option");
        option.value = String(i);
        option.textContent = labels[i];
        select.appendChild(option);
    }
};
export function bindEventAudio(label, select, source) {
    let disposed = false;
    const cleanups = [];
    const cleanup = () => {
        disposed = true;
        for (const fn of cleanups)
            fn();
        cleanups.length = 0;
        label.hidden = true;
        select.replaceChildren();
    };
    const visibility = (count) => {
        label.hidden = count < 2;
    };
    const syncSelect = (index) => {
        if (select.options.length === 0)
            return;
        const clamped = index >= 0 && index < select.options.length ? index : 0;
        if (select.selectedIndex !== clamped)
            select.selectedIndex = clamped;
    };
    if (typeof HTMLVideoElement !== "undefined" && source instanceof HTMLVideoElement) {
        const tracks = source.audioTracks;
        if (tracks) {
            const getLabels = () => Array.from({ length: tracks.length }, (_, i) => {
                const t = tracks[i];
                return t.label || t.language || `Audio ${i + 1}`;
            });
            const sync = (rebuild) => {
                const labels = getLabels();
                if (rebuild)
                    setOptions(select, labels);
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
                if (!disposed)
                    sync(true);
            };
            const onChange = () => {
                if (!disposed)
                    sync(false);
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
                for (let i = 0; i < tracks.length; i++)
                    tracks[i].enabled = i === index;
            };
            cleanups.push(() => {
                select.onchange = null;
            });
            sync(true);
        }
        else {
            visibility(0);
        }
        return cleanup;
    }
    const hls = source;
    const getLabels = () => hls.audioTracks.map((t) => t.name);
    const sync = (rebuild) => {
        const labels = getLabels();
        if (rebuild)
            setOptions(select, labels);
        visibility(labels.length);
        syncSelect(hls.audioTrack);
    };
    const onUpdated = () => {
        if (!disposed)
            sync(true);
    };
    const onSwitched = () => {
        if (!disposed)
            sync(false);
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
