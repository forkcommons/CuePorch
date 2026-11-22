import { renderSpeakerTranslations } from "./event_landing_speaker_translations.js";
import type { SpeakerProfile } from "./event_landing_speaker_lineup.js";

export type LandingTranslation = {
  title: string;
  description: string;
  published: boolean;
  speakers?: Record<string, { role: string; bio: string; base_role: string; base_bio: string }>;
};

export type LandingLocalization = {
  base: string;
  translations: Record<string, LandingTranslation>;
};

const byId = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing element #${id}`);
  return node as T;
};

export function setupLandingLocalization(profiles: () => SpeakerProfile[]) {
  const speakerContainer = document.querySelector<HTMLElement>("#eventLandingTranslatedSpeakers");
  const baseEl = byId<HTMLSelectElement>("eventLandingBase");
  const langEl = byId<HTMLSelectElement>("eventLandingLanguage");
  const fieldsEl = byId<HTMLDivElement>("eventLandingTranslationFields");
  const titleEl = byId<HTMLInputElement>("eventLandingTitle");
  const descEl = byId<HTMLTextAreaElement>("eventLandingDescription");
  const publishedEl = byId<HTMLInputElement>("eventLandingTranslationPublished");
  const removeEl = byId<HTMLButtonElement>("eventLandingRemoveTranslation");
  const previewEl = byId<HTMLAnchorElement>("eventLandingPreviewLink");

  let state: LandingLocalization = { base: "", translations: {} };
  let persisted: LandingLocalization = { base: "", translations: {} };
  let previewUrl: string | null = null;

  const active = () => langEl.value || "";

  const hasAnyTranslations = (localization: LandingLocalization) =>
    Object.keys(localization.translations).length > 0;

  const hasSavedTranslation = (lang: string) =>
    !!lang && Object.prototype.hasOwnProperty.call(persisted.translations, lang);

  const syncLanguageOptions = () => {
    const base = baseEl.value || state.base;
    for (const option of Array.from(langEl.options)) {
      const isBase = !!base && option.value === base;
      option.hidden = isBase;
      option.disabled = isBase;
    }
    if (active() && active() === base) langEl.value = "";
  };

  const syncBaseLock = () => {
    baseEl.disabled = hasAnyTranslations(persisted) || hasAnyTranslations(state);
  };

  function refreshSpeakers() {
    if (!speakerContainer) return;
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
    } catch {
      hidePreview();
    }
  };

  const ensureEntry = (lang: string) =>
    (state.translations[lang] ??= { title: "", description: "", published: false });

  const edit = (apply: (t: LandingLocalization["translations"][string]) => void) => {
    const lang = active();
    if (!lang) return;

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
    if (active() === state.base) langEl.value = "";
    syncLanguageOptions();
    syncFields();
    syncPreview();
  });

  titleEl.addEventListener("input", () => edit((t) => (t.title = titleEl.value)));
  descEl.addEventListener("input", () => edit((t) => (t.description = descEl.value)));
  publishedEl.addEventListener("change", () => edit((t) => (t.published = publishedEl.checked)));

  removeEl.addEventListener("click", () => {
    const lang = active();
    if (lang) delete state.translations[lang];
    syncFields();
    syncBaseLock();
    syncPreview();
  });

  const populate = (localization: LandingLocalization, preview: string | null) => {
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

  const payload = (): LandingLocalization => structuredClone(state);

  const applyDraft = (language: string, translation: LandingTranslation) => {
    if (language === state.base) return;
    const changed = active() !== language;
    state.translations[language] = { ...structuredClone(translation), published: false };
    langEl.value = language;
    syncFields();
    syncBaseLock();
    syncPreview();
    if (changed) langEl.dispatchEvent(new Event("change"));
  };

  return { populate, reset, payload, refreshSpeakers, applyDraft };
}
