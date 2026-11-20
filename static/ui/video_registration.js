import { checked } from "./team_api.js";
const imageFields = ["logo", "background_image"];
export function setupRegistration(call, download, credential, saved) {
    const form = document.querySelector("#registrationEditor");
    const listing = form.querySelector("#registrantRows");
    const protectedNotice = form.querySelector("#registrationProtected");
    const fields = form.querySelector("#registrationFields");
    const template = document.querySelector("#registrationField");
    const add = form.querySelector("#addRegistrationField");
    const message = form.querySelector("#registrationStatus");
    const save = form.querySelector("button[type=submit]");
    const preview = form.querySelector("#registrationPreview");
    const showPreview = form.querySelector("#showRegistrationPreview");
    const presets = form.querySelector("[name=preset]");
    const attendeeAnalytics = form.querySelector("[name=attendance_analytics]");
    const field = (node, name) => node.querySelector(`[name="${name}"]`);
    let current;
    let counter = 0;
    let images = { logo: null, background_image: null };
    let busy = false;
    const clearImage = (name) => form.querySelector(`[data-clear-image="${name}"]`);
    async function request(path, body) {
        const headers = { Authorization: "Bearer " + credential() };
        if (!(body instanceof FormData))
            headers["Content-Type"] = "application/json";
        const response = await fetch(path, { method: "POST", headers, body: body instanceof FormData ? body : JSON.stringify(body) });
        return (await checked(response)).json();
    }
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
        for (const key of ["alignment", "background", "overlay", "opacity"]) {
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
    async function action(callback) {
        message.textContent = "";
        try {
            await callback();
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
    for (const name of imageFields) {
        clearImage(name).onclick = () => {
            images[name] = null;
            field(form, name).value = "";
            clearImage(name).hidden = true;
        };
    }
    async function draft(context) {
        for (const name of imageFields) {
            const input = field(form, name);
            const file = input.files?.[0];
            if (!file)
                continue;
            const upload = new FormData();
            upload.set("logo", file);
            const uploaded = await request("/api/review-logos", upload);
            if (context !== current || form.hidden)
                return;
            if (input.files?.[0] === file) {
                images[name] = uploaded.logo;
                input.value = "";
                clearImage(name).hidden = false;
            }
        }
        const questions = [...fields.querySelectorAll(":scope > li")].map(row => ({
            id: row.dataset.fieldId, type: field(row, "kind").value, label: field(row, "label").value,
            required: field(row, "required").checked,
            options: field(row, "options").value.split("\n").map(value => value.trim()).filter(Boolean),
        }));
        return {
            enabled: field(form, "enabled").checked, collect_utm: field(form, "collect_utm").checked,
            ...(attendeeAnalytics ? { attendance_analytics: attendeeAnalytics.checked } : {}),
            heading: field(form, "heading").value, description: field(form, "description").value,
            privacy_url: field(form, "privacy_url").value, first_name: field(form, "first_name").value,
            last_name: field(form, "last_name").value, layout: field(form, "layout").value, fields: questions,
            appearance: { ...images, alignment: field(form, "alignment").value, background: field(form, "background").value,
                overlay: field(form, "overlay").value, opacity: field(form, "opacity").valueAsNumber },
        };
    }
    async function useDraft(draw) {
        if (busy)
            return;
        busy = showPreview.disabled = save.disabled = presets.disabled = true;
        const context = current;
        await action(async () => {
            const settings = await draft(context);
            if (settings && context === current && !form.hidden)
                await draw(settings, context);
        });
        busy = showPreview.disabled = save.disabled = presets.disabled = false;
    }
    const size = field(form, "preview_size");
    const screen = field(form, "preview_screen");
    size.onchange = () => { preview.dataset.size = size.value; };
    showPreview.onclick = () => {
        if (!form.reportValidity())
            return;
        void useDraft(async (settings, context) => {
            const response = await request("/api/registration/preview", { settings, screen: screen.value, event: !!attendeeAnalytics });
            if (context !== current || form.hidden)
                return;
            preview.dataset.size = size.value;
            preview.srcdoc = response.html;
            preview.hidden = false;
        });
    };
    form.onsubmit = event => {
        event.preventDefault();
        void useDraft(async (settings, context) => {
            const updated = await call("/registration", "PUT", { revision: context.revision,
                preset_name: field(form, "preset_name").value, settings });
            if (context !== current || form.hidden)
                return;
            render(updated);
            message.textContent = "Registration saved.";
            saved?.();
        });
    };
    form.querySelector("#registrationExport").onclick = () => void action(download);
    return () => action(async () => {
        render(await call("/registration"));
        form.hidden = false;
    });
}
