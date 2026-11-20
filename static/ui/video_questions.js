import { checked } from "./team_api.js";
function elapsed(seconds) {
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor(whole % 3600 / 60);
    const end = String(whole % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${end}` : `${minutes}:${end}`;
}
export function setupVideoQuestions(panel, activeVideo) {
    const form = panel.querySelector("form");
    const input = form.elements.namedItem("question");
    const language = form.elements.namedItem("language");
    const submit = form.querySelector("button[type=submit]");
    const status = panel.querySelector("[data-question-status]");
    const result = panel.querySelector("[data-question-answer]");
    const text = result.querySelector("[data-answer-text]");
    const sources = result.querySelector("[data-video-moments]");
    let generation = 0;
    form.onsubmit = async (event) => {
        event.preventDefault();
        const session = panel.dataset.session;
        if (submit.disabled || !session)
            return;
        const current = generation;
        submit.disabled = true;
        result.hidden = true;
        status.textContent = "Looking for an answer in the subtitles.";
        try {
            const response = await checked(await fetch(panel.dataset.endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CuePorch-Questions": "1" },
                body: JSON.stringify({ session, question: input.value, language: language.value }),
            }));
            const answer = await response.json();
            if (generation !== current)
                return;
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
                        const video = activeVideo();
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
            if (generation === current)
                status.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            if (generation === current)
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
    return (session) => {
        generation += 1;
        panel.dataset.session = session || "";
        panel.hidden = !session;
        panel.open = false;
        submit.disabled = false;
        result.hidden = true;
        text.textContent = status.textContent = "";
        sources.replaceChildren();
        input.value = "";
    };
}
const standalone = document.querySelector("#videoQuestions");
if (standalone)
    setupVideoQuestions(standalone, () => document.querySelector("video"));
