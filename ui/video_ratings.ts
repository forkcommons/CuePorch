interface RatingSettings { mode: string; prompt: string; start: number; }
interface Count { version: string; mode: string; score: number; responses: number; }
interface Rating { version: string; settings: RatingSettings; report: Count[]; }
type VideoRequest = <T>(suffix: string, method?: string, body?: object) => Promise<T>;

export function setupRatings(
  call: VideoRequest,
  timestamp: (value: string) => number,
  displayTime: (seconds: number) => string,
  download: () => Promise<void>,
) {
  const form = document.querySelector<HTMLFormElement>("#ratingEditor")!;
  const mode = form.querySelector<HTMLSelectElement>("[name=mode]")!;
  const details = form.querySelector<HTMLElement>("[data-rating-settings]")!;
  const prompt = form.querySelector<HTMLInputElement>("[name=prompt]")!;
  const start = form.querySelector<HTMLInputElement>("[name=start]")!;
  const counts = form.querySelector<HTMLUListElement>("#ratingCounts")!;
  const output = form.querySelector<HTMLElement>("#ratingStatus")!;
  const save = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
  let state: Rating;

  function controls() {
    details.hidden = mode.value === "none";
    prompt.required = start.required = !details.hidden;
  }

  function render(settings: Rating) {
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
    if (votes) lines.push(`Thumbs up ${up}, thumbs down ${votes - up}`);
    if (starsTotal) lines.push(`${average.toFixed(2)} out of 5 from ${starsTotal} ${starsTotal === 1 ? "rating" : "ratings"}`);
    for (const line of lines.length ? lines : ["No ratings yet."]) {
      const item = document.createElement("li");
      item.textContent = line;
      counts.append(item);
    }
  }

  async function action(callback: () => Promise<void>, success = "") {
    output.textContent = "";
    try {
      await callback();
      output.textContent = success;
    } catch (error) {
      output.textContent = error instanceof Error ? error.message : String(error);
    }
  }

  mode.onchange = controls;
  form.onsubmit = (event) => {
    event.preventDefault();
    save.disabled = true;
    void action(async () => {
      render(await call<Rating>("/rating", "PUT", {
        version: state.version, previous: state.settings,
        mode: mode.value, prompt: prompt.value, start: details.hidden ? 0 : timestamp(start.value),
      }));
    }, "Rating saved.").finally(() => { save.disabled = false; });
  };
  form.querySelector<HTMLButtonElement>("#ratingExport")!.onclick = () => void action(download);
  return () => action(async () => {
    render(await call<Rating>("/rating"));
    form.hidden = false;
  });
}
