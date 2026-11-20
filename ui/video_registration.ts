import {checked} from "./team_api.js";

interface Field {
  id: string;
  type: string;
  label: string;
  required: boolean;
  options: string[];
  parameter?: string;
  default_value?: string;
}
const imageFields = ["logo", "background_image"] as const;
type ImageField = typeof imageFields[number];
interface Appearance {
  alignment: string;
  logo: string | null;
  background_image: string | null;
  background: string;
  overlay: string;
  opacity: number;
}
interface Definition {
  enabled: boolean;
  heading: string;
  description: string;
  privacy_url: string;
  layout: string;
  first_name: string;
  last_name: string;
  collect_utm: boolean;
  attendance_analytics?: boolean;
  appearance: Appearance;
  fields: Field[];
}
interface Registrant { email: string; registered: number; answers: Record<string, string | boolean>; }
interface FormDataState {
  revision: string | null;
  settings: Definition;
  protected_public: boolean;
  presets: Record<string, Definition>;
  registrants: Registrant[];
}
type VideoRequest = <T>(suffix: string, method?: string, body?: object) => Promise<T>;

export function setupRegistration(call: VideoRequest, download: () => Promise<void>, credential: () => string,
    saved?: () => void) {
  const form = document.querySelector<HTMLFormElement>("#registrationEditor")!;
  const listing = form.querySelector<HTMLTableSectionElement>("#registrantRows")!;
  const protectedNotice = form.querySelector<HTMLElement>("#registrationProtected")!;
  const fields = form.querySelector<HTMLOListElement>("#registrationFields")!;
  const template = document.querySelector<HTMLTemplateElement>("#registrationField")!;
  const add = form.querySelector<HTMLButtonElement>("#addRegistrationField")!;
  const message = form.querySelector<HTMLElement>("#registrationStatus")!;
  const save = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
  const preview = form.querySelector<HTMLIFrameElement>("#registrationPreview")!;
  const showPreview = form.querySelector<HTMLButtonElement>("#showRegistrationPreview")!;
  const presets = form.querySelector<HTMLSelectElement>("[name=preset]")!;
  const attendeeAnalytics = form.querySelector<HTMLInputElement>("[name=attendance_analytics]");
  const field = <T extends HTMLElement = HTMLInputElement>(node: Element, name: string) =>
    node.querySelector<T>(`[name="${name}"]`)!;
  let current: FormDataState;
  let counter = 0;
  let images: Record<ImageField, string | null> = {logo: null, background_image: null};
  let busy = false;
  const clearImage = (name: ImageField) => form.querySelector<HTMLButtonElement>(`[data-clear-image="${name}"]`)!;

  async function request<T>(path: string, body: object): Promise<T> {
    const headers: Record<string, string> = {Authorization: "Bearer " + credential()};
    if (!(body instanceof FormData)) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {method: "POST", headers, body: body instanceof FormData ? body : JSON.stringify(body)});
    return (await checked(response)).json();
  }

  function append(value: Field) {
    const row = template.content.firstElementChild!.cloneNode(true) as HTMLLIElement;
    row.dataset.fieldId = value.id;
    const kind = field<HTMLSelectElement>(row, "kind");
    const label = field(row, "label");
    const options = field<HTMLTextAreaElement>(row, "options");
    const required = field(row, "required");
    const parameter = field(row, "parameter");
    const defaultValue = field(row, "default_value");
    kind.value = value.type;
    label.value = value.label;
    options.value = value.options.join("\n");
    required.checked = value.required;
    parameter.value = value.parameter ?? "";
    defaultValue.value = value.default_value ?? "";
    function controls() {
      row.querySelector<HTMLElement>("[data-field-options]")!.hidden = kind.value !== "select";
      row.querySelector<HTMLElement>("[data-field-required]")!.hidden = ["description", "hidden"].includes(kind.value);
      row.querySelector<HTMLElement>("[data-field-hidden]")!.hidden = kind.value !== "hidden";
      parameter.disabled = kind.value !== "hidden";
      parameter.required = kind.value === "hidden";
    }
    kind.onchange = controls;
    controls();
    row.querySelector<HTMLButtonElement>("[data-remove-field]")!.onclick = () => {
      row.remove();
      add.disabled = false;
    };
    fields.append(row);
    add.disabled = fields.children.length >= 20;
    return label;
  }

  function renderFields(settings: Definition) {
    const enabled = field(form, "enabled");
    enabled.checked = settings.enabled && !current.protected_public;
    enabled.disabled = current.protected_public;
    protectedNotice.hidden = !current.protected_public;
    field(form, "collect_utm").checked = settings.collect_utm;
    if (attendeeAnalytics) attendeeAnalytics.checked = settings.attendance_analytics ?? false;
    for (const key of ["heading", "description", "privacy_url", "layout", "first_name", "last_name"] as const) {
      field(form, key).value = settings[key];
    }
    for (const key of ["alignment", "background", "overlay", "opacity"] as const) {
      field(form, key).value = String(settings.appearance[key]);
    }
    for (const name of imageFields) {
      images[name] = settings.appearance[name];
      field(form, name).value = "";
      clearImage(name).hidden = !images[name];
    }
    preview.hidden = true;
    preview.removeAttribute("srcdoc");
    fields.replaceChildren();
    add.disabled = false;
    settings.fields.forEach(append);
  }

  function render(state: FormDataState) {
    current = state;
    renderFields(state.settings);
    field(form, "preset_name").value = "";
    presets.replaceChildren(new Option("Choose a preset", ""));
    for (const name of Object.keys(state.presets)) presets.add(new Option(name, name));
    listing.replaceChildren();
    for (const person of state.registrants) {
      const row = listing.insertRow();
      row.insertCell().textContent = [person.answers.first_name, person.answers.last_name].filter(Boolean).join(" ");
      row.insertCell().textContent = person.email;
      row.insertCell().textContent = new Date(person.registered * 1000).toLocaleString();
    }
  }

  async function action(callback: () => Promise<void>) {
    message.textContent = "";
    try {
      await callback();
    } catch (error) {
      message.textContent = error instanceof Error ? error.message : String(error);
    }
  }

  add.onclick = () => {
    const id = "field_" + Date.now().toString(36) + "_" + ++counter;
    append({id, type: "text", label: "", required: false, options: []}).focus();
  };
  presets.onchange = () => {
    if (presets.value) renderFields(current.presets[presets.value]);
  };

  for (const name of imageFields) {
    clearImage(name).onclick = () => {
      images[name] = null;
      field(form, name).value = "";
      clearImage(name).hidden = true;
    };
  }

  async function draft(context: FormDataState): Promise<Definition | undefined> {
    for (const name of imageFields) {
      const input = field(form, name);
      const file = input.files?.[0];
      if (!file) continue;
      const upload = new FormData();
      upload.set("logo", file);
      const uploaded = await request<{logo: string}>("/api/review-logos", upload);
      if (context !== current || form.hidden) return;
      if (input.files?.[0] === file) {
        images[name] = uploaded.logo;
        input.value = "";
        clearImage(name).hidden = false;
      }
    }
    const questions = [...fields.querySelectorAll<HTMLLIElement>(":scope > li")].map(row => ({
      id: row.dataset.fieldId!, type: field(row, "kind").value, label: field(row, "label").value,
      required: field(row, "required").checked,
      options: field(row, "options").value.split("\n").map(value => value.trim()).filter(Boolean),
      parameter: field(row, "parameter").value,
      default_value: field(row, "default_value").value,
    }));
    return {
      enabled: field(form, "enabled").checked, collect_utm: field(form, "collect_utm").checked,
      ...(attendeeAnalytics ? {attendance_analytics: attendeeAnalytics.checked} : {}),
      heading: field(form, "heading").value, description: field(form, "description").value,
      privacy_url: field(form, "privacy_url").value, first_name: field(form, "first_name").value,
      last_name: field(form, "last_name").value, layout: field(form, "layout").value, fields: questions,
      appearance: {...images, alignment: field(form, "alignment").value, background: field(form, "background").value,
        overlay: field(form, "overlay").value, opacity: field(form, "opacity").valueAsNumber},
    };
  }

  async function useDraft(draw: (settings: Definition, context: FormDataState) => Promise<void>) {
    if (busy) return;
    busy = showPreview.disabled = save.disabled = presets.disabled = true;
    const context = current;
    await action(async () => {
      const settings = await draft(context);
      if (settings && context === current && !form.hidden) await draw(settings, context);
    });
    busy = showPreview.disabled = save.disabled = presets.disabled = false;
  }

  const size = field<HTMLSelectElement>(form, "preview_size");
  const screen = field<HTMLSelectElement>(form, "preview_screen");
  size.onchange = () => { preview.dataset.size = size.value; };
  showPreview.onclick = () => {
    if (!form.reportValidity()) return;
    void useDraft(async (settings, context) => {
      const response = await request<{html: string}>("/api/registration/preview",
        {settings, screen: screen.value, event: !!attendeeAnalytics});
      if (context !== current || form.hidden) return;
      preview.dataset.size = size.value;
      preview.srcdoc = response.html;
      preview.hidden = false;
    });
  };
  form.onsubmit = event => {
    event.preventDefault();
    void useDraft(async (settings, context) => {
      const updated = await call<FormDataState>("/registration", "PUT", {revision: context.revision,
        preset_name: field(form, "preset_name").value, settings});
      if (context !== current || form.hidden) return;
      render(updated);
      message.textContent = "Registration saved.";
      saved?.();
    });
  };
  form.querySelector<HTMLButtonElement>("#registrationExport")!.onclick = () => void action(download);
  return () => action(async () => {
    render(await call<FormDataState>("/registration"));
    form.hidden = false;
  });
}
