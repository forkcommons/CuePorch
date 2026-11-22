import {groupPlayback, type GrantRequest, type State as GroupPlaybackState} from "./group_playback.js";

type Visibility = "private" | "unlisted" | "password" | "public" | "embed_only";
type Embedding = "disabled" | "anywhere" | "domains";

export interface SharingSettings {
  visibility: Visibility;
  embed: Embedding;
  domains: string[];
  download: boolean;
}

export interface Sharing extends SharingSettings {
  has_password: boolean;
  pending: SharingSettings | null;
  watch: string | null;
  embed_url: string | null;
  folder_override?: boolean;
  allowed?: {modes: Visibility[]; domains: boolean};
}

interface SharingDialog {
  title?: string;
  success?: string;
  open(title: string): HTMLElement;
  read(): Promise<Sharing>;
  save(settings: SharingSettings & { password: string }): Promise<Sharing>;
  reload(): Promise<void>;
  absolute(path: string): string;
  status: HTMLElement;
  groupPlayback?: {read(): Promise<GroupPlaybackState>; save(body: GrantRequest): Promise<GroupPlaybackState>};
}

export const privateSettings: Sharing = {
  visibility: "private", embed: "disabled", domains: [], download: false,
  has_password: false, pending: null, watch: null, embed_url: null,
};

export function sharingFields(body: HTMLElement) {
  const template = document.querySelector<HTMLTemplateElement>("#privacySettings")!;
  body.append(template.content.cloneNode(true));
  const form = body.querySelector("form")!;
  const visibility = form.querySelector<HTMLSelectElement>("[name=visibility]")!;
  const password = form.querySelector<HTMLInputElement>("[name=password]")!;
  const embed = form.querySelector<HTMLSelectElement>("[name=embed]")!;
  const domains = form.querySelector<HTMLTextAreaElement>("[name=domains]")!;
  const download = form.querySelector<HTMLInputElement>("[name=download]")!;
  const save = form.querySelector<HTMLButtonElement>("[type=submit]")!;
  let hasPassword = false;
  let allowed: Sharing["allowed"];

  function toggleFields() {
    password.closest("label")!.hidden = visibility.value !== "password";
    password.placeholder = hasPassword ? "Leave blank to keep the saved password" : "";
    password.required = visibility.value === "password" && !hasPassword;
    download.disabled = visibility.value === "private";
    embed.disabled = download.disabled || allowed?.domains === false;
    domains.closest("label")!.hidden = download.disabled || embed.value !== "domains";
    domains.disabled = domains.closest("label")!.hidden || allowed?.domains === false;
  }

  function populate(state: Sharing) {
    hasPassword = state.has_password;
    const displayed = state.pending || state;
    allowed = state.allowed;
    for (const option of visibility.options) {
      const blocked = Boolean(allowed && !allowed.modes.includes(option.value as Visibility));
      option.disabled = blocked;
      option.hidden = blocked && option.value !== displayed.visibility;
    }
    visibility.value = displayed.visibility;
    embed.value = displayed.embed;
    domains.value = displayed.domains.join("\n");
    download.checked = displayed.download;
    password.value = "";
    toggleFields();
  }

  function values() {
    return {
      visibility: visibility.value as Visibility,
      embed: embed.value as Embedding,
      domains: domains.value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean),
      download: download.checked,
      password: visibility.value === "password" ? password.value : "",
    };
  }

  visibility.onchange = embed.onchange = toggleFields;
  return { form, save, populate, values };
}

export async function openPrivacy(dialog: SharingDialog) {
  const initial = await dialog.read();
  const body = dialog.open(dialog.title || "Video privacy");
  const fields = sharingFields(body);
  const links = body.querySelector<HTMLElement>("[data-privacy-links]")!;
  const group = dialog.groupPlayback;
  const refreshGroup = group ? await groupPlayback(body, group.read, group.save) : null;

  function present(state: Sharing) {
    fields.populate(state);
    links.replaceChildren();
    for (const [label, path] of [["Watch link", state.watch], ["Embed link", state.embed_url]]) {
      if (!path) continue;
      const wrapper = document.createElement("label");
      wrapper.textContent = label;
      const input = document.createElement("input");
      input.type = "url";
      input.readOnly = true;
      input.value = dialog.absolute(path);
      wrapper.append(input);
      links.append(wrapper);
    }
    if (state.pending) dialog.status.textContent = "Waiting for the configured publication approval.";
  }

  fields.form.onsubmit = async (event) => {
    event.preventDefault();
    fields.save.disabled = true;
    dialog.status.textContent = "";
    try {
      const state = await dialog.save(fields.values());
      present(state);
      if (!state.pending) dialog.status.textContent = dialog.success || "Privacy saved.";
      await dialog.reload();
      await refreshGroup?.();
    } catch (error) {
      dialog.status.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      fields.save.disabled = false;
    }
  };
  present(initial);
}
