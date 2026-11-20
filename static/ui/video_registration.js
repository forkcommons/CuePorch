export function setupRegistration(call, download) {
    const form = document.querySelector("#registrationEditor");
    const listing = form.querySelector("#registrantRows");
    const protectedNotice = form.querySelector("#registrationProtected");
    const fields = form.querySelector("#registrationFields");
    const template = document.querySelector("#registrationField");
    const add = form.querySelector("#addRegistrationField");
    const message = form.querySelector("#registrationStatus");
    const save = form.querySelector("button[type=submit]");
    const presets = form.querySelector("[name=preset]");
    const attendeeAnalytics = form.querySelector("[name=attendance_analytics]");
    const field = (node, name) => node.querySelector(`[name="${name}"]`);
    let current;
    let counter = 0;
    function append(value) {
        const row = template.content.firstElementChild.cloneNode(true);
        row.dataset.fieldId = value.id;
        const kind = field(row, "kind");
        const label = field(row, "label");
        const options = field(row, "options");
        const required = field(row, "required");
        kind.value = value.type;
        label.value = value.label;
        options.value = value.options.join("\n");
        required.checked = value.required;
        function controls() {
            row.querySelector("[data-field-options]").hidden = kind.value !== "select";
            row.querySelector("[data-field-required]").hidden = kind.value === "description";
        }
        kind.onchange = controls;
        controls();
        row.querySelector("[data-remove-field]").onclick = () => {
            row.remove();
            add.disabled = false;
        };
        fields.append(row);
        add.disabled = fields.children.length >= 20;
        return label;
    }
    function renderFields(settings) {
        const enabled = field(form, "enabled");
        enabled.checked = settings.enabled && !current.protected_public;
        enabled.disabled = current.protected_public;
        protectedNotice.hidden = !current.protected_public;
        field(form, "collect_utm").checked = settings.collect_utm;
        if (attendeeAnalytics)
            attendeeAnalytics.checked = settings.attendance_analytics ?? false;
        for (const key of ["heading", "description", "privacy_url", "layout", "first_name", "last_name"]) {
            field(form, key).value = settings[key];
        }
        fields.replaceChildren();
        add.disabled = false;
        settings.fields.forEach(append);
    }
    function render(state) {
        current = state;
        renderFields(state.settings);
        field(form, "preset_name").value = "";
        presets.replaceChildren(new Option("Choose a preset", ""));
        for (const name of Object.keys(state.presets))
            presets.add(new Option(name, name));
        listing.replaceChildren();
        for (const person of state.registrants) {
            const row = listing.insertRow();
            row.insertCell().textContent = [person.answers.first_name, person.answers.last_name].filter(Boolean).join(" ");
            row.insertCell().textContent = person.email;
            row.insertCell().textContent = new Date(person.registered * 1000).toLocaleString();
        }
    }
    async function action(callback, success = "") {
        message.textContent = "";
        try {
            await callback();
            message.textContent = success;
        }
        catch (error) {
            message.textContent = error instanceof Error ? error.message : String(error);
        }
    }
    add.onclick = () => {
        const id = "field_" + Date.now().toString(36) + "_" + ++counter;
        append({ id, type: "text", label: "", required: false, options: [] }).focus();
    };
    presets.onchange = () => {
        if (presets.value)
            renderFields(current.presets[presets.value]);
    };
    form.onsubmit = (event) => {
        event.preventDefault();
        save.disabled = true;
        void action(async () => {
            const draft = [...fields.querySelectorAll(":scope > li")].map((row) => ({
                id: row.dataset.fieldId, type: field(row, "kind").value, label: field(row, "label").value,
                required: field(row, "required").checked,
                options: field(row, "options").value.split("\n").map((value) => value.trim()).filter(Boolean),
            }));
            render(await call("/registration", "PUT", { revision: current.revision,
                preset_name: field(form, "preset_name").value, settings: {
                    enabled: field(form, "enabled").checked, collect_utm: field(form, "collect_utm").checked,
                    ...(attendeeAnalytics ? { attendance_analytics: attendeeAnalytics.checked } : {}),
                    heading: field(form, "heading").value, description: field(form, "description").value,
                    privacy_url: field(form, "privacy_url").value, first_name: field(form, "first_name").value,
                    last_name: field(form, "last_name").value, layout: field(form, "layout").value, fields: draft,
                } }));
        }, "Registration saved.").finally(() => { save.disabled = false; });
    };
    form.querySelector("#registrationExport").onclick = () => void action(download);
    return () => action(async () => {
        render(await call("/registration"));
        form.hidden = false;
    });
}
