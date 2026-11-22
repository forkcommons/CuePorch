import { renderSpeakerTranslations } from "./event_landing_speaker_translations.js";
const byId = (id) => {
    const node = document.getElementById(id);
    if (!node)
        throw new Error(`Missing element #${id}`);
    return node;
};
export function setupLandingLocalization(profiles) {
    const speakerContainer = document.querySelector("#eventLandingTranslatedSpeakers");
    const baseEl = byId("eventLandingBase");
    const langEl = byId("eventLandingLanguage");
    const fieldsEl = byId("eventLandingTranslationFields");
    const titleEl = byId("eventLandingTitle");
    const descEl = byId("eventLandingDescription");
    const publishedEl = byId("eventLandingTranslationPublished");
    const removeEl = byId("eventLandingRemoveTranslation");
    const previewEl = byId("eventLandingPreviewLink");
    let state = { base: "", translations: {} };
    let persisted = { base: "", translations: {} };
    let previewUrl = null;
    const active = () => langEl.value || "";
    const hasAnyTranslations = (localization) => Object.keys(localization.translations).length > 0;
    const hasSavedTranslation = (lang) => !!lang && Object.prototype.hasOwnProperty.call(persisted.translations, lang);
    const syncLanguageOptions = () => {
        const base = baseEl.value || state.base;
        for (const option of Array.from(langEl.options)) {
            const isBase = !!base && option.value === base;
            option.hidden = isBase;
            option.disabled = isBase;
        }
        if (active() && active() === base)
            langEl.value = "";
    };
    const syncBaseLock = () => {
        baseEl.disabled = hasAnyTranslations(persisted) || hasAnyTranslations(state);
    };
    function refreshSpeakers() {
        if (!speakerContainer)
            return;
        const lang = active();
        if (!lang) {
            speakerContainer.replaceChildren();
            return;
        }
        renderSpeakerTranslations(speakerContainer, profiles(), state.translations[lang], () => {
            const translation = ensureEntry(lang);
            syncBaseLock();
            return translation;
        });
    }
    const syncFields = () => {
        refreshSpeakers();
        const lang = active();
        fieldsEl.hidden = !lang;
        if (!lang) {
            titleEl.value = "";
            descEl.value = "";
            publishedEl.checked = false;
            return;
        }
        const translation = state.translations[lang];
        titleEl.value = translation ? translation.title : "";
        descEl.value = translation ? translation.description : "";
        publishedEl.checked = translation ? translation.published : false;
    };
    const hidePreview = () => {
        previewEl.hidden = true;
        previewEl.removeAttribute("href");
    };
    const syncPreview = () => {
        const lang = active();
        if (!lang || !previewUrl || !hasSavedTranslation(lang)) {
            hidePreview();
            return;
        }
        try {
            const url = new URL(previewUrl, location.href);
            if (url.origin !== location.origin) {
                hidePreview();
                return;
            }
            url.searchParams.set("lang", lang);
            previewEl.href = url.toString();
            previewEl.hidden = false;
        }
        catch {
            hidePreview();
        }
    };
    const ensureEntry = (lang) => (state.translations[lang] ??= { title: "", description: "", published: false });
    const edit = (apply) => {
        const lang = active();
        if (!lang)
            return;
        apply(ensureEntry(lang));
        syncBaseLock();
    };
    langEl.addEventListener("change", () => {
        syncLanguageOptions();
        syncFields();
        syncPreview();
    });
    baseEl.addEventListener("change", () => {
        state.base = baseEl.value;
        if (active() === state.base)
            langEl.value = "";
        syncLanguageOptions();
        syncFields();
        syncPreview();
    });
    titleEl.addEventListener("input", () => edit((t) => (t.title = titleEl.value)));
    descEl.addEventListener("input", () => edit((t) => (t.description = descEl.value)));
    publishedEl.addEventListener("change", () => edit((t) => (t.published = publishedEl.checked)));
    removeEl.addEventListener("click", () => {
        const lang = active();
        if (lang)
            delete state.translations[lang];
        syncFields();
        syncBaseLock();
        syncPreview();
    });
    const populate = (localization, preview) => {
        persisted = structuredClone(localization);
        state = structuredClone(localization);
        previewUrl = preview;
        baseEl.value = state.base;
        syncLanguageOptions();
        syncBaseLock();
        syncFields();
        syncPreview();
    };
    const reset = () => {
        langEl.value = "";
        populate({ base: "en", translations: {} }, null);
    };
    const payload = () => structuredClone(state);
    return { populate, reset, payload, refreshSpeakers };
}
