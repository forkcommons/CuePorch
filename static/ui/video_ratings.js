export function setupRatings(call, timestamp, displayTime, download) {
    const form = document.querySelector("#ratingEditor");
    const mode = form.querySelector("[name=mode]");
    const details = form.querySelector("[data-rating-settings]");
    const prompt = form.querySelector("[name=prompt]");
    const start = form.querySelector("[name=start]");
    const counts = form.querySelector("#ratingCounts");
    const output = form.querySelector("#ratingStatus");
    const save = form.querySelector("button[type=submit]");
    let state;
    function controls() {
        details.hidden = mode.value === "none";
        prompt.required = start.required = !details.hidden;
    }
    function render(settings) {
        state = settings;
        mode.value = state.settings.mode;
        prompt.value = state.settings.prompt;
        start.value = displayTime(state.settings.start);
        controls();
        counts.replaceChildren();
        const thumbs = state.report.filter((row) => row.mode === "thumbs");
        const stars = state.report.filter((row) => row.mode === "stars");
        const up = thumbs.reduce((total, row) => total + row.score * row.responses, 0);
        const votes = thumbs.reduce((total, row) => total + row.responses, 0);
        const starsTotal = stars.reduce((total, row) => total + row.responses, 0);
        const average = starsTotal ? stars.reduce((total, row) => total + row.score * row.responses, 0) / starsTotal : 0;
        const lines = [];
        if (votes)
            lines.push(`Thumbs up ${up}, thumbs down ${votes - up}`);
        if (starsTotal)
            lines.push(`${average.toFixed(2)} out of 5 from ${starsTotal} ${starsTotal === 1 ? "rating" : "ratings"}`);
        for (const line of lines.length ? lines : ["No ratings yet."]) {
            const item = document.createElement("li");
            item.textContent = line;
            counts.append(item);
        }
    }
    async function action(callback, success = "") {
        output.textContent = "";
        try {
            await callback();
            output.textContent = success;
        }
        catch (error) {
            output.textContent = error instanceof Error ? error.message : String(error);
        }
    }
    mode.onchange = controls;
    form.onsubmit = (event) => {
        event.preventDefault();
        save.disabled = true;
        void action(async () => {
            render(await call("/rating", "PUT", {
                version: state.version, previous: state.settings,
                mode: mode.value, prompt: prompt.value, start: details.hidden ? 0 : timestamp(start.value),
            }));
        }, "Rating saved.").finally(() => { save.disabled = false; });
    };
    form.querySelector("#ratingExport").onclick = () => void action(download);
    return () => action(async () => {
        render(await call("/rating"));
        form.hidden = false;
    });
}
