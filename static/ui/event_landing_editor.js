import { setupSpeakerLineup } from './event_landing_speaker_lineup.js';
import { setupSpeakerProfileEditor } from './event_landing_speaker_editor.js';
export function setupEventLandingEditor(request) {
    const details = document.querySelector('#eventLanding');
    const form = document.querySelector('#eventLandingForm');
    const statusEl = document.querySelector('#eventLandingStatus');
    const saveBtn = document.querySelector('#saveEventLanding');
    if (!details || !form || !statusEl || !saveBtn)
        return () => { };
    const get = (n) => form.elements.namedItem(n);
    const speakerFields = document.querySelector("#eventLandingSpeakerFields");
    const lineup = setupSpeakerLineup((profile) => editor.open(profile));
    const editor = setupSpeakerProfileEditor(request, (profile) => lineup.upsert(profile));
    document.querySelector("#eventLandingSpeakerNew")?.addEventListener("click", () => editor.open());
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
    };
    const fill = (s, ps) => {
        get('enabled').checked = s.enabled;
        get('theme').value = s.theme;
        get('layout').value = s.layout;
        get('accent').value = s.accent;
        get('show_logo').checked = s.show_logo;
        get('show_poster').checked = s.show_poster;
        lineup.populate(s.speaker_ids, ps);
    };
    const read = () => ({
        enabled: get('enabled').checked,
        theme: get('theme').value,
        layout: get('layout').value,
        accent: get('accent').value,
        show_logo: get('show_logo').checked,
        show_poster: get('show_poster').checked,
        speaker_ids: lineup.selected(),
    });
    const url = (t) => '/api/live-events/' + encodeURIComponent(t) + '/landing';
    const reset = () => {
        cache = null;
        busy = false;
        setSaveEnabled();
        form.reset();
        editor.reset();
        lineup.populate([], []);
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
            fill(res.settings, res.profiles);
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
            fill(res.settings, res.profiles);
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
