type Request = <T>(path: string, method?: string, body?: object) => Promise<T>;

interface CsvPreview {
  headers: string[];
  fields: {id: string; label: string; required: boolean}[];
  mapping: Record<string, number>;
  rows: number;
  revision: string;
}

export function setupAttendeeCsv(request: Request) {
  const panel = document.querySelector<HTMLDetailsElement>("#eventCsvSettings")!;
  const form = document.querySelector<HTMLFormElement>("#eventCsvForm")!;
  const input = form.elements.namedItem("attendees") as HTMLInputElement;
  const fields = document.querySelector<HTMLFieldSetElement>("#eventCsvColumns")!;
  const mapping = document.querySelector("#eventCsvMapping")!;
  const status = document.querySelector("#eventCsvStatus")!;
  let destination = "";
  let generation = 0;
  let revision = "";

  function reset() {
    fields.hidden = true;
    fields.disabled = true;
    mapping.replaceChildren();
    status.textContent = "";
  }

  input.onchange = async () => {
    const ticket = ++generation;
    reset();
    if (!destination || !input.files?.length) return;
    const body = new FormData(form);
    body.set("preview", "1");
    status.textContent = "Reading the CSV…";
    try {
      const preview = await request<CsvPreview>(destination, "POST", body);
      if (ticket !== generation) return;
      revision = preview.revision;
      for (const field of preview.fields) {
        const label = document.createElement("label");
        label.textContent = field.label;
        const column = document.createElement("select");
        column.name = field.id;
        column.required = field.required;
        column.append(new Option(field.required ? "Choose a column" : "Skip", ""));
        preview.headers.forEach((text, index) => column.append(new Option(text, String(index))));
        column.value = preview.mapping[field.id]?.toString() ?? "";
        label.append(column);
        mapping.append(label);
      }
      fields.hidden = false;
      fields.disabled = false;
      status.textContent = `${preview.rows} attendee ${preview.rows === 1 ? "row" : "rows"}. Check the column matches before importing.`;
    } catch (error) {
      if (ticket === generation) status.textContent = (error as Error).message;
    }
  };

  form.onsubmit = async event => {
    event.preventDefault();
    if (fields.disabled || !input.files?.length) return;
    const ticket = generation;
    const body = new FormData();
    body.set("attendees", input.files[0]);
    body.set("revision", revision);
    body.set("mapping", JSON.stringify(Object.fromEntries(Array.from(mapping.querySelectorAll("select"))
      .filter(column => column.value !== "").map(column => [column.name, Number(column.value)]))));
    fields.disabled = true;
    input.disabled = true;
    status.textContent = "Importing attendees…";
    try {
      const result = await request<{added: number; existing: number; duplicates: number}>(destination, "POST", body);
      if (ticket === generation) {
        const noun = result.duplicates === 1 ? "row" : "rows";
        const repeats = result.duplicates ? `, ${result.duplicates} duplicate ${noun} combined` : "";
        status.textContent = `${result.added} added, ${result.existing} already registered${repeats}.`;
      }
    } catch (error) {
      if (ticket === generation) status.textContent = (error as Error).message;
    } finally {
      if (ticket === generation) {
        fields.disabled = false;
        input.disabled = false;
      }
    }
  };

  return (event: string, occurrence: number) => {
    const path = `/api/live-events/${event}/${occurrence}/registration/import`;
    if (destination === path) return;
    destination = path;
    generation++;
    panel.open = false;
    form.reset();
    input.disabled = false;
    reset();
  };
}
