const form = document.querySelector(".video-rating");
const video = form.closest(".interactive-player").querySelector("video");
const message = form.querySelector("[data-rating-message]");
const buttons = [...form.querySelectorAll("button")];
const start = Number(form.dataset.start);
let submitted = false;
function update() {
    form.hidden = video.ended || video.currentTime < start || (video.paused && video.currentTime === 0);
}
for (const event of ["play", "timeupdate", "seeked", "ended"])
    video.addEventListener(event, update);
form.onsubmit = async (event) => {
    event.preventDefault();
    if (submitted)
        return;
    const button = event.submitter;
    if (!button)
        return;
    submitted = true;
    buttons.forEach((choice) => { choice.disabled = true; });
    try {
        const response = await fetch(form.dataset.rate, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ score: Number(button.value) }),
        });
        if (!response.ok)
            throw new Error();
        message.textContent = "Rating recorded.";
    }
    catch {
        message.textContent = "The rating could not be saved.";
        buttons.forEach((choice) => { choice.disabled = false; });
        submitted = false;
    }
};
update();
export {};
