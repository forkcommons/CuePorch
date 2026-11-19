export const playerSwitches = ["customize", "progress", "volume", "pip", "fullscreen", "show_title"];
export const playerColors = ["primary", "accent", "icon"];
export function eventControls(player, title, initial) {
    let appearance = initial;
    let refresh = () => { };
    const template = document.querySelector("#eventControls");
    function setAppearance(value) {
        appearance = value;
        title.hidden = value.customize && !value.show_title;
        refresh();
    }
    function attach(video, live) {
        const controls = template.content.firstElementChild.cloneNode(true);
        const get = (name) => controls.querySelector(`[data-control="${name}"]`);
        const play = get("play");
        const position = get("position");
        const clock = get("clock");
        const mute = get("mute");
        const volume = get("volume");
        const fullscreen = get("fullscreen");
        const pip = get("pip");
        const status = get("status");
        let started = !video.paused;
        player.append(controls);
        function seekable() {
            if (!live)
                return Number.isFinite(video.duration) && video.duration > 0 ? [0, video.duration] : null;
            const last = video.seekable.length - 1;
            return last >= 0 ? [video.seekable.start(last), video.seekable.end(last)] : null;
        }
        function elapsed(seconds) {
            const whole = Math.max(0, Math.floor(seconds));
            return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
        }
        function syncPosition() {
            const range = seekable();
            position.disabled = !range;
            if (!range) {
                clock.textContent = "";
                return;
            }
            const [start, end] = range;
            position.min = String(start);
            position.max = String(end);
            position.value = String(video.currentTime);
            const label = live ? `${elapsed(end - video.currentTime)} behind live` : elapsed(video.currentTime);
            position.setAttribute("aria-valuetext", label);
            clock.textContent = live ? label : `${label} / ${elapsed(end)}`;
        }
        function syncPlayback() {
            play.textContent = video.paused ? "Play" : "Pause";
            player.dataset.state = started ? (video.paused ? "paused" : "playing") : "ready";
        }
        function syncVolume() {
            mute.textContent = video.muted ? "Unmute" : "Mute";
            volume.value = String(video.muted ? 0 : video.volume);
        }
        function fail(error) {
            status.textContent = error instanceof Error ? error.message : String(error);
        }
        play.onclick = () => {
            status.textContent = "";
            if (video.paused)
                void video.play().catch(fail);
            else
                video.pause();
        };
        position.oninput = () => { video.currentTime = position.valueAsNumber; };
        mute.onclick = () => { video.muted = !video.muted; };
        volume.oninput = () => {
            video.volume = volume.valueAsNumber;
            video.muted = video.volume === 0;
        };
        fullscreen.onclick = () => {
            const change = document.fullscreenElement ? document.exitFullscreen() : player.requestFullscreen();
            void change.catch(fail);
        };
        pip.onclick = () => {
            const change = document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture();
            void change.catch(fail);
        };
        video.addEventListener("play", () => { started = true; syncPlayback(); });
        video.addEventListener("pause", syncPlayback);
        video.addEventListener("ended", syncPlayback);
        for (const event of ["timeupdate", "loadedmetadata", "progress"])
            video.addEventListener(event, syncPosition);
        video.addEventListener("volumechange", syncVolume);
        refresh = () => {
            const { customize, progress, live_seek } = appearance;
            player.classList.toggle("archive-player", customize);
            player.dataset.playPosition = appearance.play_position;
            for (const key of playerColors)
                player.style.setProperty(`--viewer-${key}`, appearance[key]);
            controls.hidden = !customize;
            video.controls = !customize;
            video.disablePictureInPicture = customize && !appearance.pip;
            position.closest("label").hidden = !progress || (live && !live_seek);
            clock.hidden = position.closest("label").hidden;
            mute.hidden = !appearance.volume;
            volume.closest("label").hidden = !appearance.volume;
            fullscreen.hidden = !appearance.fullscreen || !document.fullscreenEnabled;
            pip.hidden = !appearance.pip || !document.pictureInPictureEnabled;
            syncPlayback();
            syncPosition();
            syncVolume();
        };
        refresh();
    }
    function reset() {
        refresh = () => { };
        player.classList.remove("archive-player");
    }
    setAppearance(initial);
    return { setAppearance, attach, reset };
}
