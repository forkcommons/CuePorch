import { setupLibraryMailchimp } from "./library_mailchimp_registration.js";
import { setupLibraryListDelivery } from "./library_list_registration.js";
import { checked } from "./team_api.js";
const imageFields = ["logo", "background_image"];
export function setupRegistration(call, download, credential, saved, registrationEndpoint, downloadVimeoHistory) {
    const form = document.querySelector("#registrationEditor");
    const mailchimpPanel = form.querySelector("#registrationMailchimp");
    const mailchimp = mailchimpPanel && registrationEndpoint
        ? setupLibraryMailchimp(mailchimpPanel, credential) : undefined;
    const listDeliveryHandlers = [
        ["constant-contact", "#registrationConstantContact"],
        ["campaign-monitor", "#registrationCampaignMonitor"],
        ["keap", "#registrationKeap"],
        ["salesforce", "#registrationSalesforce"],
    ].flatMap(([provider, id]) => {
        const panel = form.querySelector(id);
        return panel && registrationEndpoint
            ? [[provider, setupLibraryListDelivery(panel, credential, (endpoint) => endpoint === registrationEndpoint() + "/" + provider + "/outbound", provider)]]
            : [];
    });
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
    const timing = form.querySelector('select[name="timing"]');
    const timeSeconds = form.querySelector('input[name="time_seconds"]');
    const registrationTime = form.querySelector("#registrationTime");
    const enabledLabel = form.querySelector("[data-registration-enabled-label]");
    const field = (node, name) => node.querySelector(`[name="${name}"]`);
    const language = field(form, "translation_language");
    const translate = field(form, "translation_enabled");
    const translated = form.querySelector("#registrationTranslation");
    const localizedHeading = field(form, "translated_heading");
    const localizedDescription = field(form, "translated_description");
    const localizedFields = form.querySelector("#registrationTranslatedFields");
    const previewLanguage = field(form, "preview_language");
    let current;
    let counter = 0;
    let images = { logo: null, background_image: null };
    let translations = {};
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
        const parameter = field(row, "parameter");
        const defaultValue = field(row, "default_value");
        const terms = field(row, "link");
        kind.value = value.type;
        label.value = value.label;
        options.value = value.options.join("\n");
        required.checked = value.required;
        parameter.value = value.parameter ?? "";
        defaultValue.value = value.default_value ?? "";
        terms.value = value.link ?? "";
        function controls() {
            row.querySelector("[data-field-options]").hidden = kind.value !== "select";
            row.querySelector("[data-field-required]").hidden = ["description", "hidden"].includes(kind.value);
            row.querySelector("[data-field-hidden]").hidden = kind.value !== "hidden";
            parameter.disabled = kind.value !== "hidden";
            parameter.required = kind.value === "hidden";
            row.querySelector("[data-field-link]").hidden = kind.value !== "checkbox";
            terms.disabled = kind.value !== "checkbox";
        }
        kind.onchange = controls;
        controls();
        row.querySelector("[data-remove-field]").onclick = () => {
            row.remove();
            add.disabled = false;
            renderTranslation();
        };
        fields.append(row);
        add.disabled = fields.children.length >= 20;
        return label;
    }
    function readFields() {
        return [...fields.querySelectorAll(":scope > li")].map(row => ({
            id: row.dataset.fieldId, type: field(row, "kind").value, label: field(row, "label").value,
            required: field(row, "required").checked,
            options: field(row, "options").value.split("\n").map(value => value.trim()).filter(Boolean),
            parameter: field(row, "parameter").value, default_value: field(row, "default_value").value,
            link: field(row, "link").value,
        }));
    }
    function renderTranslation() {
        const draft = translations[language.value];
        translate.checked = Boolean(draft);
        translated.hidden = !draft;
        const previous = previewLanguage.value;
        const active = Array.from(language.options).filter(option => option.value in translations);
        previewLanguage.replaceChildren(new Option("English", "en"), ...active.map(option => new Option(option.textContent, option.value)));
        previewLanguage.value = previous in translations ? previous : "en";
        if (!draft)
            return;
        localizedHeading.value = draft.heading;
        localizedDescription.value = draft.description;
        localizedHeading.oninput = () => { draft.heading = localizedHeading.value; };
        localizedDescription.oninput = () => { draft.description = localizedDescription.value; };
        const controls = [];
        function input(title, value, limit, update) {
            const label = document.createElement("label");
            const element = document.createElement("input");
            element.maxLength = limit;
            element.value = value;
            element.oninput = () => update(element.value);
            label.append(title, " ", element);
            controls.push(label);
        }
        for (const base of readFields()) {
            if (base.type === "hidden" || !base.label.trim())
                continue;
            const entry = draft.fields[base.id] ??= { label: "", options: {} };
            input("Translation for " + base.label, entry.label, 240, value => { entry.label = value; });
            if (base.type === "select") {
                for (const option of base.options) {
                    const label = Object.hasOwn(entry.options, option) ? entry.options[option] : "";
                    input("Translation for choice " + option, label, 80, value => { entry.options = { ...entry.options, [option]: value }; });
                }
            }
        }
        localizedFields.replaceChildren(...controls);
    }
    language.onchange = renderTranslation;
    translate.onchange = () => {
        if (translate.checked)
            translations[language.value] = { heading: "", description: "", fields: {} };
        else
            delete translations[language.value];
        renderTranslation();
    };
    fields.addEventListener("input", renderTranslation);
    function updateTiming() {
        if (!timing || !timeSeconds || !registrationTime || !enabledLabel)
            return;
        const during = timing.value === "during";
        registrationTime.hidden = !during;
        timeSeconds.required = during;
        enabledLabel.textContent = timing.value === "before" ? "Require registration before playback" : "Show registration form";
    }
    timing?.addEventListener("change", updateTiming);
    function renderFields(settings) {
        const enabled = field(form, "enabled");
        enabled.checked = settings.enabled && !current.protected_public;
        enabled.disabled = current.protected_public;
        protectedNotice.hidden = !current.protected_public;
        field(form, "collect_utm").checked = settings.collect_utm;
        if (attendeeAnalytics)
            attendeeAnalytics.checked = settings.attendance_analytics ?? false;
        if (timing)
            timing.value = settings.timing || "before";
        if (timeSeconds)
            timeSeconds.value = settings.time_seconds ? String(settings.time_seconds) : "";
        updateTiming();
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
        translations = structuredClone(settings.translations ?? {});
        renderTranslation();
    }
    function render(state, endpoint) {
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
        if (mailchimp && endpoint !== undefined)
            void mailchimp(endpoint + "/mailchimp/outbound", state.settings);
        if (endpoint !== undefined)
            for (const [provider, handler] of listDeliveryHandlers)
                void handler(endpoint + "/" + provider + "/outbound", state.settings);
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
        const questions = readFields();
        const localized = Object.fromEntries(Object.entries(translations).map(([code, draft]) => {
            const labels = {};
            for (const question of questions) {
                const entry = draft.fields[question.id];
                if (!entry || question.type === "hidden")
                    continue;
                const choices = question.type === "select" ? question.options : [];
                const options = choices.filter(value => Object.hasOwn(entry.options, value)).map(value => [value, entry.options[value]]);
                labels[question.id] = { label: entry.label, options: Object.fromEntries(options) };
            }
            return [code, { ...draft, fields: labels }];
        }));
        return {
            enabled: field(form, "enabled").checked, collect_utm: field(form, "collect_utm").checked,
            ...(attendeeAnalytics ? { attendance_analytics: attendeeAnalytics.checked } : {}),
            ...(timing && timeSeconds ? {
                timing: timing.value,
                time_seconds: timing.value === "during" ? timeSeconds.valueAsNumber : 0,
            } : {}),
            heading: field(form, "heading").value, description: field(form, "description").value,
            privacy_url: field(form, "privacy_url").value, first_name: field(form, "first_name").value,
            last_name: field(form, "last_name").value, layout: field(form, "layout").value, fields: questions,
            translations: localized,
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
            const response = await request("/api/registration/preview", { settings, screen: screen.value, language: previewLanguage.value, event: !!attendeeAnalytics });
            if (context !== current || form.hidden)
                return;
            preview.dataset.size = size.value;
            preview.srcdoc = response.html;
            preview.hidden = false;
        });
    };
    form.onsubmit = event => {
        event.preventDefault();
        const endpoint = registrationEndpoint?.();
        void useDraft(async (settings, context) => {
            if (context !== current || form.hidden || endpoint !== registrationEndpoint?.())
                return;
            const updated = await call("/registration", "PUT", { revision: context.revision,
                preset_name: field(form, "preset_name").value, settings });
            if (context !== current || form.hidden || endpoint !== registrationEndpoint?.())
                return;
            render(updated, endpoint);
            message.textContent = "Registration saved.";
            saved?.();
        });
    };
    form.querySelector("#registrationExport").onclick = () => void action(download);
    const vimeoHistoryPanel = form.querySelector("#registrationVimeoHistory");
    const vimeoHistoryFile = form.querySelector("#vimeoHistoryFile");
    const vimeoHistoryUpload = form.querySelector("#vimeoHistoryUpload");
    const vimeoHistoryDownload = form.querySelector("#vimeoHistoryDownload");
    if (vimeoHistoryPanel && vimeoHistoryFile && vimeoHistoryUpload && vimeoHistoryDownload && downloadVimeoHistory) {
        vimeoHistoryUpload.onclick = () => void action(async () => {
            const file = vimeoHistoryFile.files?.[0];
            if (!file)
                throw new Error("Choose a Vimeo registrant CSV.");
            const endpoint = registrationEndpoint?.();
            if (!endpoint)
                return;
            const data = new FormData();
            data.append("registrants", file);
            const result = await request(endpoint + "/vimeo-history", data);
            if (endpoint !== registrationEndpoint?.())
                return;
            message.textContent = `Archived ${result.archive} Vimeo registration${result.archive === 1 ? "" : "s"}.`;
            vimeoHistoryFile.value = "";
        });
        vimeoHistoryDownload.onclick = () => void action(downloadVimeoHistory);
    }
    return () => action(async () => {
        const endpoint = registrationEndpoint?.();
        const state = await call("/registration");
        if (endpoint !== registrationEndpoint?.())
            return;
        render(state, endpoint);
        if (vimeoHistoryPanel)
            vimeoHistoryPanel.hidden = state.vimeo_history !== true;
        form.hidden = false;
    });
}
