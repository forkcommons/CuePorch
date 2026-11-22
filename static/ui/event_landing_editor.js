import { setupSpeakerLineup } from './event_landing_speaker_lineup.js';
import { setupSpeakerProfileEditor } from './event_landing_speaker_editor.js';
import { setupLandingLocalization } from './event_landing_locale.js';
export function setupEventLandingEditor(request) {
    const details = document.querySelector('#eventLanding');
    const form = document.querySelector('#eventLandingForm');
    const statusEl = document.querySelector('#eventLandingStatus');
    const saveBtn = document.querySelector('#saveEventLanding');
    if (!details || !form || !statusEl || !saveBtn)
        return () => { };
    const get = (n) => form.elements.namedItem(n);
    const speakerFields = document.querySelector("#eventLandingSpeakerFields");
    const localeFields = document.querySelector("#eventLandingLocale");
    const lineup = setupSpeakerLineup((profile) => editor.open(profile), () => locale.refreshSpeakers());
    const editor = setupSpeakerProfileEditor(request, (profile) => lineup.upsert(profile));
    document.querySelector("#eventLandingSpeakerNew")?.addEventListener("click", () => editor.open());
    const locale = setupLandingLocalization(() => lineup.selectedProfiles());
    let uid = null;
    let gen = 0;
    let cache = null;
    let busy = false;
    const setStatus = (m) => {
        statusEl.textContent = m;
    };
    const setSaveEnabled = () => {
        saveBtn.disabled = busy || !cache;
        speakerFields.disabled = saveBtn.disabled;
        localeFields.disabled = saveBtn.disabled;
    };
    const fill = (s) => {
        get('enabled').checked = s.settings.enabled;
        get('theme').value = s.settings.theme;
        get('layout').value = s.settings.layout;
        get('accent').value = s.settings.accent;
        get('show_logo').checked = s.settings.show_logo;
        get('show_poster').checked = s.settings.show_poster;
        lineup.populate(s.settings.speaker_ids, s.profiles);
        locale.populate(s.settings.localization, s.preview);
    };
    const read = () => ({
        enabled: get('enabled').checked,
        theme: get('theme').value,
        layout: get('layout').value,
        accent: get('accent').value,
        show_logo: get('show_logo').checked,
        show_poster: get('show_poster').checked,
        speaker_ids: lineup.selected(),
        localization: locale.payload(),
    });
    const url = (t) => '/api/live-events/' + encodeURIComponent(t) + '/landing';
    const reset = () => {
        cache = null;
        busy = false;
        setSaveEnabled();
        form.reset();
        editor.reset();
        lineup.populate([], []);
        locale.reset();
        setStatus('');
        details.open = false;
    };
    setSaveEnabled();
    const load = async (target, myGen) => {
        if (busy)
            return;
        busy = true;
        setSaveEnabled();
        setStatus('Loading…');
        try {
            const res = await request(url(target));
            if (gen !== myGen || uid !== target)
                return;
            cache = res;
            fill(res);
            setStatus('');
        }
        catch (e) {
            if (gen === myGen && uid === target) {
                cache = null;
                setStatus(e instanceof Error ? e.message : 'Failed to load');
            }
        }
        finally {
            if (gen === myGen && uid === target) {
                busy = false;
                setSaveEnabled();
            }
        }
    };
    form.onsubmit = async (ev) => {
        ev.preventDefault();
        if (busy || !uid || !cache)
            return;
        const target = uid;
        const myGen = gen;
        const revision = cache.revision;
        busy = true;
        setSaveEnabled();
        setStatus('Saving…');
        try {
            const res = await request(url(target), 'PUT', {
                revision,
                settings: read(),
            });
            if (gen !== myGen || uid !== target)
                return;
            cache = res;
            fill(res);
            setStatus('Saved');
        }
        catch (e) {
            if (gen !== myGen || uid !== target)
                return;
            // The generic request helper only throws an Error string, so a 409
            // cannot be distinguished here. Show the message, keep the current
            // unsaved controls, and drop the stale cache so that reopening the
            // details panel will fetch fresh server state.
            cache = null;
            setStatus(e instanceof Error ? e.message : 'Failed to save');
        }
        finally {
            if (gen === myGen && uid === target) {
                busy = false;
                setSaveEnabled();
            }
        }
    };
    details.addEventListener('toggle', () => {
        if (!details.open)
            return;
        if (!uid || cache || busy)
            return;
        void load(uid, gen);
    });
    return (nextUid) => {
        if (nextUid === uid)
            return;
        uid = nextUid;
        gen++;
        reset();
    };
}
