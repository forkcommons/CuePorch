import { setupSpeakerLineup, type SpeakerProfile } from './event_landing_speaker_lineup.js';
import { setupSpeakerProfileEditor } from './event_landing_speaker_editor.js';

type Settings = {
  enabled: boolean; theme: 'light' | 'dark'; layout: 'stacked' | 'split';
  accent: string; show_logo: boolean; show_poster: boolean;
  speaker_ids: string[];
};
type ServerState = { revision: string | null; settings: Settings; profiles: SpeakerProfile[] };
type Request = <T>(path: string, method?: string, body?: object) => Promise<T>;

export function setupEventLandingEditor(request: Request) {
  const details = document.querySelector<HTMLDetailsElement>('#eventLanding');
  const form = document.querySelector<HTMLFormElement>('#eventLandingForm');
  const statusEl = document.querySelector<HTMLElement>('#eventLandingStatus');
  const saveBtn = document.querySelector<HTMLButtonElement>('#saveEventLanding');
  if (!details || !form || !statusEl || !saveBtn) return () => {};

  const get = <T extends HTMLElement>(n: string) => form.elements.namedItem(n) as T;

  const speakerFields = document.querySelector<HTMLFieldSetElement>("#eventLandingSpeakerFields")!;
  const lineup = setupSpeakerLineup((profile) => editor.open(profile));
  const editor = setupSpeakerProfileEditor(request, (profile) => lineup.upsert(profile));
  document.querySelector("#eventLandingSpeakerNew")?.addEventListener("click", () => editor.open());

  let uid: string | null = null;
  let gen = 0;
  let cache: ServerState | null = null;
  let busy = false;

  const setStatus = (m: string) => {
    statusEl.textContent = m;
  };

  const setSaveEnabled = () => {
    saveBtn.disabled = busy || !cache;
    speakerFields.disabled = saveBtn.disabled;
  };

  const fill = (s: Settings, ps: SpeakerProfile[]) => {
    get<HTMLInputElement>('enabled').checked = s.enabled;
    get<HTMLSelectElement>('theme').value = s.theme;
    get<HTMLSelectElement>('layout').value = s.layout;
    get<HTMLInputElement>('accent').value = s.accent;
    get<HTMLInputElement>('show_logo').checked = s.show_logo;
    get<HTMLInputElement>('show_poster').checked = s.show_poster;
    lineup.populate(s.speaker_ids, ps);
  };

  const read = (): Settings => ({
    enabled: get<HTMLInputElement>('enabled').checked,
    theme: get<HTMLSelectElement>('theme').value as Settings['theme'],
    layout: get<HTMLSelectElement>('layout').value as Settings['layout'],
    accent: get<HTMLInputElement>('accent').value,
    show_logo: get<HTMLInputElement>('show_logo').checked,
    show_poster: get<HTMLInputElement>('show_poster').checked,
    speaker_ids: lineup.selected(),
  });

  const url = (t: string) =>
    '/api/live-events/' + encodeURIComponent(t) + '/landing';

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

  const load = async (target: string, myGen: number) => {
    if (busy) return;
    busy = true;
    setSaveEnabled();
    setStatus('Loading…');
    try {
      const res = await request<ServerState>(url(target));
      if (gen !== myGen || uid !== target) return;
      cache = res;
      fill(res.settings, res.profiles);
      setStatus('');
    } catch (e) {
      if (gen === myGen && uid === target) {
        cache = null;
        setStatus(e instanceof Error ? e.message : 'Failed to load');
      }
    } finally {
      if (gen === myGen && uid === target) {
        busy = false;
        setSaveEnabled();
      }
    }
  };

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    if (busy || !uid || !cache) return;

    const target = uid;
    const myGen = gen;
    const revision = cache.revision;

    busy = true;
    setSaveEnabled();
    setStatus('Saving…');

    try {
      const res = await request<ServerState>(url(target), 'PUT', {
        revision,
        settings: read(),
      });
      if (gen !== myGen || uid !== target) return;
      cache = res;
      fill(res.settings, res.profiles);
      setStatus('Saved');
    } catch (e) {
      if (gen !== myGen || uid !== target) return;
      // The generic request helper only throws an Error string, so a 409
      // cannot be distinguished here. Show the message, keep the current
      // unsaved controls, and drop the stale cache so that reopening the
      // details panel will fetch fresh server state.
      cache = null;
      setStatus(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      if (gen === myGen && uid === target) {
        busy = false;
        setSaveEnabled();
      }
    }
  };

  details.addEventListener('toggle', () => {
    if (!details.open) return;
    if (!uid || cache || busy) return;

    void load(uid, gen);
  });

  return (nextUid: string) => {
    if (nextUid === uid) return;
    uid = nextUid;
    gen++;
    reset();
  };
}
