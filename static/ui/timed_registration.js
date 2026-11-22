const video = document.querySelector("video");
const panel = document.querySelector("[data-timed-registration]");
const heading = panel.querySelector("#registrationHeading");
const dismiss = panel.querySelector("[data-timed-registration-dismiss]");
const form = panel.querySelector("form");
const resumeInput = panel.querySelector('input[name="resume_at"]');
const timing = panel.dataset.timedRegistration;
const target = Number(panel.dataset.registrationTime);
const originalLoop = video.loop;
let wasPlaying = false;
let opened = !panel.hidden;
if (timing === "after")
    video.loop = false;
const open = () => {
    if (opened)
        return;
    opened = true;
    wasPlaying = !video.paused;
    video.pause();
    panel.hidden = false;
    heading.tabIndex = -1;
    heading.focus();
};
if (timing === "during") {
    const check = () => {
        if (video.currentTime > 0 && video.currentTime >= Math.min(target, video.duration))
            open();
    };
    video.addEventListener("timeupdate", check);
    video.addEventListener("seeked", check);
}
else if (timing === "after") {
    video.addEventListener("ended", open);
}
video.addEventListener("play", () => {
    if (!panel.hidden)
        video.pause();
});
if (!panel.hidden)
    video.pause();
dismiss.addEventListener("click", () => {
    panel.hidden = true;
    video.loop = originalLoop;
    video.focus();
    if (timing === "during" && wasPlaying)
        video.play().catch(() => { });
});
form.addEventListener("submit", () => {
    if (resumeInput)
        resumeInput.value = video.currentTime.toFixed(3);
});
export {};
