import type { SpeakerProfile } from "./event_landing_speaker_lineup.js";

type RequestFn = <T>(path: string, method?: string, body?: object) => Promise<T>;

export function setupSpeakerProfileEditor(
  request: RequestFn,
  onSaved: (p: SpeakerProfile) => void,
): { open(profile?: SpeakerProfile): void; reset(): void } {
  const dialog = document.getElementById("eventLandingSpeakerDialog") as HTMLDialogElement | null;
  const form = document.getElementById("eventLandingSpeakerForm") as HTMLFormElement | null;
  const status = document.getElementById("eventLandingSpeakerStatus");
  const save = document.getElementById("eventLandingSpeakerSave") as HTMLButtonElement | null;
  const cancel = document.getElementById("eventLandingSpeakerCancel") as HTMLButtonElement | null;
  const nameField = form?.elements.namedItem("name") as HTMLInputElement | null;
  const roleField = form?.elements.namedItem("role") as HTMLInputElement | null;
  const bioField = form?.elements.namedItem("bio") as HTMLTextAreaElement | null;

  if (!dialog || !form || !status || !save || !cancel || !nameField || !roleField || !bioField) {
    return { open() {}, reset() {} };
  }

  let current: SpeakerProfile | null = null;
  let token = 0;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (save.disabled) return;

    const name = nameField.value.trim();
    if (!name) {
      status.textContent = "Name is required.";
      nameField.focus();
      return;
    }

    const profile = current;
    const fields = {
      name,
      role: roleField.value.trim(),
      bio: bioField.value.trim(),
    };
    const body = profile ? { ...fields, revision: profile.revision } : fields;

    const mine = ++token;
    save.disabled = true;
    status.textContent = "Saving…";

    const path = profile
      ? `/api/live-event-speaker-profiles/${encodeURIComponent(String(profile.id))}`
      : "/api/live-event-speaker-profiles";

    request<SpeakerProfile>(path, profile ? "PUT" : "POST", body)
      .then((saved) => {
        if (mine !== token) return;
        current = null;
        dialog.close();
        onSaved(saved);
      })
      .catch((error: unknown) => {
        if (mine !== token) return;
        const detail = error instanceof Error && error.message ? error.message : "Save failed";
        status.textContent = `${detail}. Try again.`;
      })
      .finally(() => {
        if (mine === token) save.disabled = false;
      });
  });

  cancel.addEventListener("click", () => {
    reset();
  });

  dialog.addEventListener("cancel", (event) => {
    if (save.disabled) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    reset();
  });

  const open = (profile?: SpeakerProfile) => {
    token++;
    current = profile ?? null;
    nameField.value = profile?.name ?? "";
    roleField.value = profile?.role ?? "";
    bioField.value = profile?.bio ?? "";
    status.textContent = "";
    save.disabled = false;
    dialog.showModal();
  };

  const reset = () => {
    token++;
    current = null;
    save.disabled = false;
    status.textContent = "";
    form.reset();
    if (dialog.open) dialog.close();
  };

  return { open, reset };
}
