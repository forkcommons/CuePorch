import type { Translation } from './event_series_localization.js';

type Entry = {
  id: string;
  revision: string;
};

type Job<T> = {
  id: string;
  revision: string | null;
  language: string;
  state: string;
  result: T | null;
  error: string | null;
};

type JobsResponse<T> = {
  available: boolean;
  jobs: Job<T>[];
};

type GenerateResponse<T> = {
  job: Job<T>;
};

export function setupTranslationJobs<T>(
  root: HTMLElement,
  api: <R>(suffix: string, method?: string, body?: object) => Promise<R>,
  applyDraft: (language: string, translation: T) => void,
  reloadEntry: () => Promise<void>,
  options: { controlPrefix: string; languageSelect: HTMLSelectElement; jobPath: (id: string) => string },
) {
  const q = <E extends HTMLElement>(selector: string) => root.querySelector<E>(selector)!;
  const { controlPrefix, languageSelect, jobPath } = options;

  const generateButton = q<HTMLButtonElement>(`#${controlPrefix}AiGenerate`);
  const refreshButton  = q<HTMLButtonElement>(`#${controlPrefix}AiRefresh`);
  const draftButton    = q<HTMLButtonElement>(`#${controlPrefix}AiDraft`);
  const reloadButton   = q<HTMLButtonElement>(`#${controlPrefix}AiReload`);
  const automaticCheckbox = q<HTMLInputElement>(`#${controlPrefix}AiAuto`);
  const statusElement  = q<HTMLElement>(`#${controlPrefix}AiStatus`);

  automaticCheckbox.checked = true;

  let entry: Entry | null = null;
  let jobs: Job<T>[] = [];
  let available = false;
  let generating: Entry | null = null;

  const selectedLanguage = () => languageSelect.value;

  const currentJob = (): Job<T> | null => {
    return jobs.find((job) => job.language === selectedLanguage()) ?? null;
  };

  const render = () => {
    const job = currentJob();
    const isCurrentRevision = !!entry && !!job && job.revision === entry.revision;

    draftButton.hidden = !(isCurrentRevision && job?.state === 'draft' && job.result);
    reloadButton.hidden = !(entry && job?.state === 'saved' && job.revision !== entry.revision);

    refreshButton.disabled = !entry;
    generateButton.disabled = !entry || !selectedLanguage() || !available || generating === entry;

    if (!entry) {
      statusElement.textContent = '';
      return;
    }

    if (!available) {
      statusElement.textContent = 'Translation jobs unavailable.';
      return;
    }

    if (!job) {
      statusElement.textContent = 'No job for this language.';
      return;
    }

    const stateLabel = job.state === 'waiting' ? 'Waiting for translation approval.' : job.state;
    statusElement.textContent = job.error || stateLabel;
  };

  const load = async () => {
    const target = entry;
    if (!target) {
      jobs = [];
      available = false;
      render();
      return;
    }

    try {
      const response = await api<JobsResponse<T>>(
        jobPath(target.id),
        'GET',
      );

      if (entry !== target) return;

      available = response.available;
      jobs = Array.isArray(response.jobs) ? response.jobs : [];
      render();
    } catch (error) {
      if (entry !== target) return;

      available = false;
      jobs = [];
      render();
      statusElement.textContent =
        error instanceof Error ? error.message : 'Failed to load translation jobs.';
    }
  };

  const generate = async () => {
    const target = entry;
    const language = selectedLanguage();
    if (!target || !language || generating === target) return;

    generating = target;
    render();
    try {
      statusElement.textContent = 'Generating…';
      const response = await api<GenerateResponse<T>>(
        jobPath(target.id),
        'POST',
        {
          revision: target.revision,
          language,
          apply_now: automaticCheckbox.checked,
        },
      );

      if (entry !== target) return;

      const index = jobs.findIndex((job) => job.language === response.job.language);
      if (index >= 0) {
        jobs = jobs.map((job, i) => (i === index ? response.job : job));
      } else {
        jobs = [...jobs, response.job];
      }

      render();
    } catch (error) {
      if (entry !== target) return;

      render();
      statusElement.textContent =
        error instanceof Error ? error.message : 'Generation failed.';
    } finally {
      if (generating === target) {
        generating = null;
      }
      generateButton.disabled = !entry || !selectedLanguage() || !available || generating === entry;
    }
  };

  const applySelectedJobDraft = () => {
    const job = currentJob();
    if (!entry || !job) return;
    if (job.revision !== entry.revision) return;
    if (job.state !== 'draft') return;
    if (!job.result) return;

    applyDraft(job.language, job.result);
  };

  languageSelect.addEventListener('change', render);
  automaticCheckbox.addEventListener('change', render);
  generateButton.addEventListener('click', generate);
  refreshButton.addEventListener('click', load);
  draftButton.addEventListener('click', applySelectedJobDraft);
  reloadButton.addEventListener('click', async () => {
    const target = entry;
    if (!target) return;
    await reloadEntry();
    if (entry !== target) return;
    await load();
  });

  return {
    populate(next: Entry | null) {
      entry = next;
      jobs = [];
      available = false;
      render();
      if (next) {
        void load();
      }
    },
  };
}

export function setupSeriesTranslationJobs(
  root: HTMLElement,
  api: <T>(suffix: string, method?: string, body?: object) => Promise<T>,
  applyDraft: (language: string, translation: Translation) => void,
  reloadEntry: () => Promise<void>,
) {
  const languageSelect = root.querySelector<HTMLSelectElement>('#seriesTranslationLanguage')!;
  return setupTranslationJobs<Translation>(root, api, applyDraft, reloadEntry, {
    controlPrefix: 'series',
    languageSelect,
    jobPath: (id) => `/${id}/translation-jobs`,
  });
}
