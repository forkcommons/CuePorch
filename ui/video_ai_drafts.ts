type Step = "ai_details" | "ai_chapters" | "ai_highlights";
interface GeneratedDetails {title: string; description: string; tags: string[]}
interface Chapter {timecode: number; title: string}
interface Reel {work: string; title: string; duration: number}
interface Job {
  id: string; state: string; result: GeneratedDetails | {chapters: Chapter[]} | Reel | null; error: string | null;
}
interface Report {
  version: string; tracks: string[]; original_language: string; configured: boolean;
  jobs: Partial<Record<Step, Job>>; restrictions: Record<Step, string | null>;
}
type Request = <T>(route: string, method?: string, body?: object) => Promise<T>;

const active = ["queued", "waiting", "running"];
const messages: Record<string, string> = {
  queued: "Generation queued.", waiting: "Waiting for operator approval.", running: "Generating with the local model.",
  saved: "Generated result saved.", draft: "Draft ready. Edit it here before saving.",
  rejected: "The operator rejected this job.",
};

export function videoAIDrafts(call: Request, updateDetails: (details: GeneratedDetails) => void) {
  const panel = document.querySelector<HTMLElement>("#aiVideoTools")!;
  const track = document.querySelector<HTMLSelectElement>("#aiVideoTrack")!;
  const auto = document.querySelector<HTMLInputElement>("#aiVideoAuto")!;
  const notice = document.querySelector<HTMLElement>("#aiVideoStatus")!;
  const tools = Array.from(panel.querySelectorAll<HTMLFormElement>("form[data-ai-step]"), form => ({
    form,
    step: form.dataset.aiStep as Step,
    run: form.querySelector<HTMLButtonElement>("button[data-generate]")!,
    result: form.querySelector<HTMLFieldSetElement>("[data-result]")!,
    status: form.querySelector<HTMLElement>("[data-state]")!,
  }));
  let state: Report;
  let poll: number | undefined;

  function display(next: Report) {
    const previous = state;
    state = next;
    clearTimeout(poll);
    const selected = track.value;
    track.replaceChildren(...next.tracks.map(language => new Option(language, language)));
    if (next.tracks.includes(selected)) track.value = selected;
    else if (next.tracks.includes(next.original_language)) track.value = next.original_language;
    track.disabled = next.tracks.length === 0;
    notice.textContent = !next.configured ? "Configure a local Ollama model to generate drafts."
      : !next.tracks.length ? "Add a saved subtitle track in Languages and subtitles." : "";
    for (const {form, step, run, result, status} of tools) {
      const job = next.jobs[step];
      const prior = previous?.jobs[step];
      const restriction = next.restrictions[step];
      run.disabled = Boolean(restriction || !next.configured || !track.value || (job && active.includes(job.state)));
      status.textContent = restriction || (job ? job.error || messages[job.state] || "" : "");
      result.hidden = job?.state !== "draft";
      result.disabled = Boolean(restriction);
      if (step === "ai_highlights" && job?.state === "saved" && job.result && "work" in job.result) {
        const path = "/operator/videos/" + encodeURIComponent(job.result.work);
        result.querySelector<HTMLAnchorElement>("[data-reel-preview]")!.href = path;
        result.querySelector<HTMLAnchorElement>("[data-reel-editor]")!.href = path + "/timeline";
        result.hidden = false;
        status.textContent = "Private highlight reel saved.";
      }
      if (job?.state === "draft" && job.result && result.dataset.draft !== job.id) {
        if ("chapters" in job.result) {
          const list = form.querySelector<HTMLElement>("[data-chapters]")!;
          list.replaceChildren(...job.result.chapters.map(({title, timecode}, index) => {
            const row = document.createElement("fieldset");
            row.dataset.chapter = "";
            const legend = document.createElement("legend");
            legend.textContent = `Chapter ${index + 1}`;
            const start = document.createElement("input");
            start.type = "number";
            start.min = "0";
            start.step = "1";
            start.required = true;
            start.valueAsNumber = timecode;
            const name = document.createElement("input");
            name.maxLength = 50;
            name.required = true;
            name.value = title;
            const startLabel = document.createElement("label");
            startLabel.append("Start (seconds)", start);
            const titleLabel = document.createElement("label");
            titleLabel.append("Chapter title", name);
            const remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "Remove chapter";
            remove.onclick = () => row.remove();
            row.append(legend, startLabel, titleLabel, remove);
            return row;
          }));
        } else if ("description" in job.result) {
          (form.elements.namedItem("title") as HTMLInputElement).value = job.result.title;
          (form.elements.namedItem("description") as HTMLTextAreaElement).value = job.result.description;
          (form.elements.namedItem("tags") as HTMLTextAreaElement).value = job.result.tags.join("\n");
        }
        result.dataset.draft = job.id;
      }
      if (step === "ai_details" && prior && prior.state !== "saved" && job?.state === "saved"
          && job.result && "description" in job.result) updateDetails(job.result);
    }
    panel.hidden = false;
    if (Object.values(next.jobs).some(job => active.includes(job.state))) {
      poll = window.setTimeout(() => void load(), 1000);
    }
  }

  async function load() {
    try {
      display(await call<Report>("/ai-drafts"));
    } catch (error) {
      if (!panel.hidden) notice.textContent = error instanceof Error ? error.message : String(error);
    }
  }

  for (const {form, step, run, status} of tools) {
    run.onclick = async () => {
      if (step === "ai_highlights" && !form.reportValidity()) return;
      run.disabled = true;
      try {
        display(await call<Report>("/ai-drafts", "POST", {
          version: state.version, step, source_language: track.value, apply: auto.checked,
          ...(step === "ai_highlights" ? {prompt: new FormData(form).get("prompt")} : {}),
        }));
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
        run.disabled = false;
      }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      const job = state.jobs[step];
      if (job?.state !== "draft") return;
      const submit = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
      const data = new FormData(form);
      const result = step === "ai_details" ? {
        title: data.get("title"), description: data.get("description"),
        tags: String(data.get("tags")).split("\n").map(tag => tag.trim()).filter(Boolean),
      } : {chapters: Array.from(form.querySelectorAll<HTMLElement>("[data-chapter]"), row => ({
        timecode: row.querySelector<HTMLInputElement>("input[type=number]")!.valueAsNumber,
        title: row.querySelector<HTMLInputElement>("input:not([type])")!.value,
      }))};
      submit.disabled = true;
      try {
        display(await call<Report>("/ai-drafts", "PUT", {version: state.version, step, id: job.id, result}));
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        submit.disabled = false;
      }
    };
  }
  return load;
}
