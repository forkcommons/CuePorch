const player = document.querySelector(".archive-player");
const video = player.querySelector("video");
const controls = player.querySelector(".archive-controls");
const control = (id) => controls.querySelector(`[data-control="${id}"]`);
const play = control("play");
const state = control("status");
const mute = control("mute");
const volume = control("volume");
const position = control("position");
const clock = control("clock");
const speed = control("speed");
const captions = control("captions");
const fullscreen = control("fullscreen");
const pip = control("pip");
let furthest = Number(player.dataset.start || 0);
let started = !video.paused;
function fail(error) {
    state.textContent = error instanceof Error ? error.message : String(error);
}
function time(seconds) {
    const whole = Math.floor(seconds);
    return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
}
function updatePlayback() {
    play.textContent = video.paused ? (play.dataset.play ?? "Play") : (play.dataset.pause ?? "Pause");
    player.dataset.state = started ? (video.paused ? "paused" : "playing") : "ready";
}
function updatePosition() {
    if (video.seeking && player.dataset.seek === "no")
        return;
    furthest = Math.max(furthest, video.currentTime);
    if (!Number.isFinite(video.duration))
        return;
    if (position) {
        position.max = String(video.duration);
        position.value = String(video.currentTime);
        position.setAttribute("aria-valuetext", time(video.currentTime));
    }
    if (clock)
        clock.textContent = time(video.currentTime) + " / " + time(video.duration);
}
function updateVolume() {
    if (mute)
        mute.textContent = video.muted ? (mute.dataset.unmute ?? "Unmute") : (mute.dataset.mute ?? "Mute");
    if (volume)
        volume.value = String(video.muted ? 0 : video.volume);
}
play.onclick = () => {
    state.textContent = "";
    if (video.paused)
        void video.play().catch(fail);
    else
        video.pause();
};
if (position)
    position.oninput = () => { video.currentTime = position.valueAsNumber; };
if (volume)
    volume.oninput = () => { video.volume = volume.valueAsNumber; video.muted = video.volume === 0; };
if (mute)
    mute.onclick = () => { video.muted = !video.muted; };
if (speed)
    speed.onchange = () => { video.playbackRate = Number(speed.value); };
if (captions)
    captions.onchange = () => {
        for (const [index, textTrack] of Array.from(video.textTracks).entries()) {
            textTrack.mode = String(index) === captions.value ? "showing" : "disabled";
        }
    };
if (fullscreen && document.fullscreenEnabled) {
    fullscreen.hidden = false;
    fullscreen.onclick = () => {
        const change = document.fullscreenElement ? document.exitFullscreen() : player.requestFullscreen();
        void change.catch(fail);
    };
}
if (pip && document.pictureInPictureEnabled) {
    pip.hidden = false;
    pip.onclick = () => {
        const change = document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture();
        void change.catch(fail);
    };
}
video.addEventListener("play", () => { started = true; updatePlayback(); });
video.addEventListener("pause", updatePlayback);
video.addEventListener("ended", updatePlayback);
video.addEventListener("timeupdate", updatePosition);
video.addEventListener("loadedmetadata", updatePosition);
video.addEventListener("volumechange", updateVolume);
if (player.dataset.context === "no")
    video.addEventListener("contextmenu", (event) => event.preventDefault());
if (player.dataset.seek === "no") {
    video.addEventListener("seeking", () => {
        if (video.currentTime > furthest + .05)
            video.currentTime = furthest;
    });
}
updatePlayback();
updatePosition();
updateVolume();
video.controls = false;
controls.hidden = false;
export {};
