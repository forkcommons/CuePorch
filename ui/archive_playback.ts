export {};

const player = document.querySelector<HTMLElement>(".archive-player")!;
const video = player.querySelector<HTMLVideoElement>("video")!;
const controls = player.querySelector<HTMLElement>(".archive-controls")!;
const control = <T extends HTMLElement>(id: string) => controls.querySelector<T>(`[data-control="${id}"]`);
const play = control<HTMLButtonElement>("play")!;
const state = control<HTMLElement>("status")!;
const mute = control<HTMLButtonElement>("mute");
const volume = control<HTMLInputElement>("volume");
const position = control<HTMLInputElement>("position");
const clock = control<HTMLElement>("clock");
const speed = control<HTMLSelectElement>("speed");
const captions = control<HTMLSelectElement>("captions");
const fullscreen = control<HTMLButtonElement>("fullscreen");
const pip = control<HTMLButtonElement>("pip");
let furthest = Number(player.dataset.start || 0);
let started = !video.paused;

function fail(error: unknown) {
  state.textContent = error instanceof Error ? error.message : String(error);
}

function time(seconds: number) {
  const whole = Math.floor(seconds);
  return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
}

function updatePlayback() {
  play.textContent = video.paused ? (play.dataset.play ?? "Play") : (play.dataset.pause ?? "Pause");
  player.dataset.state = started ? (video.paused ? "paused" : "playing") : "ready";
}

function updatePosition() {
  if (video.seeking && player.dataset.seek === "no") return;
  furthest = Math.max(furthest, video.currentTime);
  if (!Number.isFinite(video.duration)) return;
  if (position) {
    position.max = String(video.duration);
    position.value = String(video.currentTime);
    position.setAttribute("aria-valuetext", time(video.currentTime));
  }
  if (clock) clock.textContent = time(video.currentTime) + " / " + time(video.duration);
}

function updateVolume() {
  if (mute) mute.textContent = video.muted ? (mute.dataset.unmute ?? "Unmute") : (mute.dataset.mute ?? "Mute");
  if (volume) volume.value = String(video.muted ? 0 : video.volume);
}

play.onclick = () => {
  state.textContent = "";
  if (video.paused) void video.play().catch(fail);
  else video.pause();
};
if (position) position.oninput = () => { video.currentTime = position.valueAsNumber; };
if (volume) volume.oninput = () => { video.volume = volume.valueAsNumber; video.muted = video.volume === 0; };
if (mute) mute.onclick = () => { video.muted = !video.muted; };
if (speed) speed.onchange = () => { video.playbackRate = Number(speed.value); };
if (captions) captions.onchange = () => {
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
if (player.dataset.context === "no") video.addEventListener("contextmenu", (event) => event.preventDefault());
if (player.dataset.seek === "no") {
  video.addEventListener("seeking", () => {
    if (video.currentTime > furthest + .05) video.currentTime = furthest;
  });
}
updatePlayback();
updatePosition();
updateVolume();
video.controls = false;
controls.hidden = false;
