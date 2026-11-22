export function setupSpeakerProfileEditor(request, onSaved) {
    const dialog = document.getElementById("eventLandingSpeakerDialog");
    const form = document.getElementById("eventLandingSpeakerForm");
    const status = document.getElementById("eventLandingSpeakerStatus");
    const save = document.getElementById("eventLandingSpeakerSave");
    const cancel = document.getElementById("eventLandingSpeakerCancel");
    const nameField = form?.elements.namedItem("name");
    const roleField = form?.elements.namedItem("role");
    const bioField = form?.elements.namedItem("bio");
    if (!dialog || !form || !status || !save || !cancel || !nameField || !roleField || !bioField) {
        return { open() { }, reset() { } };
    }
    let current = null;
    let token = 0;
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (save.disabled)
            return;
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
        request(path, profile ? "PUT" : "POST", body)
            .then((saved) => {
            if (mine !== token)
                return;
            current = null;
            dialog.close();
            onSaved(saved);
        })
            .catch((error) => {
            if (mine !== token)
                return;
            const detail = error instanceof Error && error.message ? error.message : "Save failed";
            status.textContent = `${detail}. Try again.`;
        })
            .finally(() => {
            if (mine === token)
                save.disabled = false;
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
    const open = (profile) => {
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
        if (dialog.open)
            dialog.close();
    };
    return { open, reset };
}
