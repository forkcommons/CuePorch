export function setupSeriesTranslationJobs(root, api, applyDraft, reloadEntry) {
    const q = (selector) => root.querySelector(selector);
    const languageSelect = q('#seriesTranslationLanguage');
    const generateButton = q('#seriesAiGenerate');
    const refreshButton = q('#seriesAiRefresh');
    const draftButton = q('#seriesAiDraft');
    const reloadButton = q('#seriesAiReload');
    const automaticCheckbox = q('#seriesAiAuto');
    const statusElement = q('#seriesAiStatus');
    automaticCheckbox.checked = true;
    let entry = null;
    let jobs = [];
    let available = false;
    let generating = null;
    const selectedLanguage = () => languageSelect.value;
    const currentJob = () => {
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
            const response = await api(`/${target.id}/translation-jobs`, 'GET');
            if (entry !== target)
                return;
            available = response.available;
            jobs = Array.isArray(response.jobs) ? response.jobs : [];
            render();
        }
        catch (error) {
            if (entry !== target)
                return;
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
        if (!target || !language || generating === target)
            return;
        generating = target;
        render();
        try {
            statusElement.textContent = 'Generating…';
            const response = await api(`/${target.id}/translation-jobs`, 'POST', {
                revision: target.revision,
                language,
                apply_now: automaticCheckbox.checked,
            });
            if (entry !== target)
                return;
            const index = jobs.findIndex((job) => job.language === response.job.language);
            if (index >= 0) {
                jobs = jobs.map((job, i) => (i === index ? response.job : job));
            }
            else {
                jobs = [...jobs, response.job];
            }
            render();
        }
        catch (error) {
            if (entry !== target)
                return;
            render();
            statusElement.textContent =
                error instanceof Error ? error.message : 'Generation failed.';
        }
        finally {
            if (generating === target) {
                generating = null;
            }
            generateButton.disabled = !entry || !selectedLanguage() || !available || generating === entry;
        }
    };
    const applySelectedJobDraft = () => {
        const job = currentJob();
        if (!entry || !job)
            return;
        if (job.revision !== entry.revision)
            return;
        if (job.state !== 'draft')
            return;
        if (!job.result)
            return;
        applyDraft(job.language, job.result);
    };
    languageSelect.addEventListener('change', render);
    automaticCheckbox.addEventListener('change', render);
    generateButton.addEventListener('click', generate);
    refreshButton.addEventListener('click', load);
    draftButton.addEventListener('click', applySelectedJobDraft);
    reloadButton.addEventListener('click', async () => {
        const target = entry;
        if (!target)
            return;
        await reloadEntry();
        if (entry !== target)
            return;
        await load();
    });
    return {
        populate(next) {
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
