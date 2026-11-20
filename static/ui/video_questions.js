import { checked } from "./team_api.js";
const panel = document.querySelector("#videoQuestions");
const form = panel.querySelector("form");
const input = form.elements.namedItem("question");
const language = form.elements.namedItem("language");
const submit = form.querySelector("button[type=submit]");
const status = panel.querySelector("[data-question-status]");
const result = panel.querySelector("[data-question-answer]");
const text = result.querySelector("[data-answer-text]");
const sources = result.querySelector("[data-video-moments]");
const video = document.querySelector("video");
function elapsed(seconds) {
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor(whole % 3600 / 60);
    const end = String(whole % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${end}` : `${minutes}:${end}`;
}
form.onsubmit = async (event) => {
    event.preventDefault();
    if (submit.disabled)
        return;
    submit.disabled = true;
    result.hidden = true;
    status.textContent = "Looking for an answer in the subtitles.";
    try {
        const response = await checked(await fetch("/api/viewer/video-questions", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CuePorch-Questions": "1" },
            body: JSON.stringify({ session: panel.dataset.session, question: input.value, language: language.value }),
        }));
        const answer = await response.json();
        if (!answer.answer) {
            status.textContent = "The subtitles do not contain an answer to this question.";
            return;
        }
        text.textContent = answer.answer;
        sources.replaceChildren(...answer.moments.map(({ seconds, quote }) => {
            const row = document.createElement("li");
            if (answer.seek) {
                const jump = document.createElement("button");
                jump.type = "button";
                jump.textContent = "Play from " + elapsed(seconds);
                jump.onclick = () => {
                    video.currentTime = seconds;
                    void video.play().catch(() => { status.textContent = "Press play to watch from the cited moment."; });
                };
                row.append(jump, " ");
            }
            else {
                row.append(elapsed(seconds) + " ");
            }
            row.append(quote);
            return row;
        }));
        status.textContent = "Answer ready.";
        result.hidden = false;
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        submit.disabled = false;
    }
};
for (const button of form.querySelectorAll("[data-question]")) {
    button.onclick = () => {
        input.value = button.dataset.question;
        form.requestSubmit();
    };
}
form.inert = false;
