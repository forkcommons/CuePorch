import {setupRegistration} from "./video_registration.js";
import {ChatState, messageAction, renderChat} from "./event_chat.js";
import {setupProducerEngagement} from "./event_engagement.js";
import {relaySummary, type RelayHealth} from "./event_health.js";
import {waitingPlayer} from "./event_waiting.js";
import {playerColors, playerSwitches, type EventAppearance} from "./event_controls.js";
import {setupLiveApi} from "./live_api.js";
import {setupEventMail} from "./event_mail.js";
import {setupAttendeeCsv} from "./attendee_csv.js";
import {setupEventRoster} from "./event_roster.js";
import {interpreterControls} from "./event_interpreters.js";

type Repeat = "none" | "days" | "weeks" | "months";
interface Schedule {
  timezone: string; start: string; end: string; repeat: Repeat; interval: number;
  weekdays: number[]; until: string | null; count: number | null;
}
interface EventSettings {
  title: string; description: string; type: string; visibility: string;
  latency: string; save_replay: boolean; archive_replay?: boolean; dvr_seconds?: number; schedule: Schedule;
  setup?: Partial<Pick<StartingOptions, "method" | "chat" | "captions" | "qa">>;
  embed_policy?: {mode: string; domains: string[]};
  regional_policy?: RegionalPolicy;
  content_rating?: Classification;
}
interface RegionalPolicy {mode: "anywhere" | "allow" | "deny"; areas: string[];}
interface Classification {rating: "unrated" | "all" | "mature"; content: string[]; advertisement: boolean; realistic_ai: boolean;}
interface StartingOptions {
  method: "browser" | "encoder";
  registration: boolean;
  preview_encoder: boolean;
  chat: {enabled: boolean; interval: number};
  captions: {enabled: boolean; language: string};
  qa: {enabled: boolean; anonymous: boolean; review: boolean};
}
interface Occurrence {id: number; start: string; end: string; local: string; delivery: string; program: string | null;}
interface LiveEvent {
  id: string; slug: string; revision: string; editable: boolean; event: EventSettings; publication: string;
  occurrences: Occurrence[]; watch: string | null; embed: string | null;
  preview_encoder: boolean;
}
interface Calendar {events: LiveEvent[]; overlaps: string[]; host_ready: boolean; direct_rtmp: boolean;}
interface Encoder {
  server: string; secure_server?: string; key: string; public: boolean; preview: boolean;
  backup_key?: string; output_height?: number;
}
interface EncoderPreview {state: "inactive" | "waiting" | "signal" | "ended" | "released"; address: string | null;}
interface PreviewHls {loadSource(address: string): void; attachMedia(video: HTMLVideoElement): void; destroy(): void;}
type PreviewHlsFactory = {new (config: object): PreviewHls; isSupported(): boolean};
interface EventHealth extends RelayHealth {
  state: string; viewers?: number;
  delivery_settings?: {latency: string | null; dvr_seconds: number | null};
}
interface AudienceMetrics {
  current: number; peak: number; loads: number; plays: number;
  watched_seconds: number; average_seconds_per_load: number;
}
interface ProductionCaptions {enabled: boolean; language: string; status: string; cue_count: number;}
interface ReplayCaptions {state: string; language: string; revision: string; has_track: boolean; published: boolean; archived: boolean;}
interface LibraryRecording {state: string; work: string | null; error: string | null;}
interface PlaylistOptions {enabled: boolean; autoplay_next: boolean; loop: boolean;}
interface EventPlayerOptions extends Omit<EventAppearance, "live_seek"> {
  show_schedule: boolean; show_recurring: boolean; show_latest: boolean;
  poster: string | null; image: string | null; recurring: string;
  logo: string | null; logo_image: string | null; byline: string;
  show_live_badge: boolean; show_viewers: boolean;
}
interface Destination {id: string; name: string; server: string; enabled: boolean; state: string; opens: number | null; provider?: string | null; preset?: string | null; provider_phase?: string | null; reusable?: boolean; configured_enabled: boolean; skipped: boolean;}
interface Destinations {available: boolean; destinations: Destination[];}
interface YouTubeBroadcast {id: string; title: string; channel: string; starts: string; privacy: string;}
interface YouTubeBroadcasts {items: YouTubeBroadcast[]; next: string | null;}
interface SrtListener {
  server: string; passphrase: string; key_bytes: number; preview: boolean;
  backup_server?: string; backup_passphrase?: string;
}
interface ProgramSource {work: string; cut: string;}
interface ProgramChoice extends ProgramSource {title: string;}
interface EventProgram {
  revision: string | null; state: string; error: string | null; choices: ProgramChoice[];
  sources: ProgramSource[]; editable: boolean;
}

const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const input = (id: string) => element<HTMLInputElement>(id);
const button = (id: string) => element<HTMLButtonElement>(id);
const identify = element<HTMLFormElement>("eventsIdentify");
const credential = identify.elements.namedItem("credential") as HTMLInputElement;
const status = element("eventsStatus");
const workspace = element("eventsWorkspace");
const form = element<HTMLFormElement>("newEventForm");
const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;
const mode = element<HTMLSelectElement>("eventMode");
const preset = element<HTMLSelectElement>("eventPreset");
const startingOptions: StartingOptions = {method: "browser", registration: false, preview_encoder: false,
  chat: {enabled: false, interval: 0}, captions: {enabled: false, language: "en"},
  qa: {enabled: false, anonymous: false, review: false}};
const typeDefaults: Record<string, {latency: string; chat?: boolean; captions?: boolean; registration?: boolean; qa?: boolean; method?: "encoder"}> = {
  live: {latency: "standard", chat: true, captions: true, method: "encoder"},
  webinar: {latency: "standard", chat: true, captions: true, registration: true, qa: true},
  team: {latency: "low", chat: true, captions: true},
  learning: {latency: "standard", captions: true, registration: true, qa: true},
  custom: {latency: "standard"},
};
const output = element("eventProduction");
const health = element("eventHealth");
const measurements = element("eventHealthDetails");
const analytics = element<HTMLDetailsElement>("eventAnalytics");
const analyticsRows = element<HTMLTableSectionElement>("eventAnalyticsRows");
const analyticsStatus = element("eventAnalyticsStatus");
const captionSettings = element<HTMLDetailsElement>("eventCaptionSettings");
const captionForm = element<HTMLFormElement>("productionCaptions");
const captionEnabled = captionForm.elements.namedItem("enabled") as HTMLInputElement;
const captionLanguage = captionForm.elements.namedItem("language") as HTMLSelectElement;
const captionStatus = element("productionCaptionStatus");
const replayCaptions = element<HTMLDetailsElement>("eventReplayCaptions");
const replayCaptionForm = element<HTMLFormElement>("replaceReplayCaptions");
const replayCaptionStatus = element("replayCaptionStatus");
const libraryRecording = element<HTMLDetailsElement>("eventLibraryRecording");
const libraryRecordingStatus = element("libraryRecordingStatus");
const libraryRecordingLink = element<HTMLAnchorElement>("libraryRecordingLink");
const saveLibraryRecording = button("saveLibraryRecording");
const eventPlaylistPanel = element<HTMLDetailsElement>("eventPlaylistSettings");
const eventPlaylistForm = element<HTMLFormElement>("eventPlaylistForm");
const eventPlaylistStatus = element("eventPlaylistStatus");
const playlistOptions = ["enabled", "autoplay_next", "loop"] as const;
const playlistOption = (name: keyof PlaylistOptions) => eventPlaylistForm.elements.namedItem(name) as HTMLInputElement;
const eventPlayerPanel = element<HTMLDetailsElement>("eventPlayerSettings");
const eventPlayerForm = element<HTMLFormElement>("eventPlayerForm");
const eventPlayerStatus = element("eventPlayerStatus");
const playerOption = (name: string) => eventPlayerForm.elements.namedItem(name) as HTMLInputElement;
const appearanceFields = element<HTMLFieldSetElement>("eventAppearanceOptions");
const playPosition = eventPlayerForm.elements.namedItem("play_position") as HTMLSelectElement;
const previewWaiting = waitingPlayer(eventPlayerPanel);
const eventLogo = element<HTMLImageElement>("eventLogoPreview");
const playerArtwork = {poster: button("removeEventPoster"), logo: button("removeEventLogo")};
const artworkFields = ["poster", "logo"] as const;
const displaySwitches = ["show_schedule", "show_recurring", "show_latest", "show_live_badge", "show_viewers"] as const;
type ArtworkField = typeof artworkFields[number];
let playerOptions: EventPlayerOptions | undefined;
const artworkPreview = {poster: "", logo: ""};
const removedArtwork = new Set<ArtworkField>();
let replayCaptionRevision = "";
const destinations = element<HTMLDetailsElement>("eventDestinations");
const destinationList = element("destinationList");
const destinationForm = element<HTMLFormElement>("addDestination");
const destinationStatus = element("destinationStatus");
const youtubePanel = element("youtubeBroadcasts");
const youtubeList = element<HTMLSelectElement>("youtubeBroadcastList");
const youtubeStatus = element("youtubeBroadcastStatus");
const youtubeMore = button("youtubeBroadcastsMore");
const youtubeForm = element<HTMLFormElement>("addYouTubeDestination");
const youtubeSubmit = button("addYouTubeDestinationButton");
const youtubeCreate = element<HTMLFormElement>("createYouTubeBroadcast");
const youtubeCreateButton = button("createYouTubeBroadcastButton");
const youtubeSeriesLabel = element("youtubeSeriesLabel");
const youtubeSeriesInput = element<HTMLInputElement>("youtubeSeries");
const youtubeSeriesSkip = button("youtubeSeriesSkip");
const youtubeSeriesStop = button("youtubeSeriesStop");
const youtubeAddSeriesLabel = element("youtubeAddSeriesLabel");
const youtubeAddSeries = element<HTMLInputElement>("youtubeAddSeries");
let youtubeNext: string | null = null;
const facebookForm = element<HTMLFormElement>("addFacebookLiveDestination");
const facebookPage = element<HTMLSelectElement>("facebookLivePage");
const facebookSubmit = button("prepareFacebookLive");
const facebookAutomatic = input("facebookAutomatic");
const facebookCancel = button("cancelFacebookAuto");
const facebookRestoreDate = button("facebookRestoreDate");
const facebookSeries = input("facebookSeries");
const facebookSeriesControls = element("facebookSeriesControls");
const facebookSeriesStop = button("facebookSeriesStop");
const facebookStatus = element("facebookLiveStatus");
let facebookStatusRevision = 0;
const facebookTarget = element<HTMLSelectElement>("facebookLiveTarget");
const facebookPageLabel = element("facebookLivePageLabel");
const facebookPrivacy = element<HTMLSelectElement>("facebookLivePrivacy");
const facebookPrivacyLabel = element("facebookLivePrivacyLabel");
const facebookLoad = button("loadFacebookLivePages");
function clearFacebookAsyncStatus() {
  facebookStatusRevision++;
  facebookStatus.textContent = "";
  facebookCancel.disabled = true;
  facebookRestoreDate.hidden = true;
  facebookSeriesStop.disabled = true;
  facebookPrivacy.value = "";
}
function facebookProfile() { return facebookTarget.value === "profile"; }
function facebookBasePath() { return mediaPath(facebookProfile() ? "destinations/facebook-profile" : "destinations/facebook"); }
const linkedinForm = element<HTMLFormElement>("linkedinLiveSchedule");
const linkedinAccount = element<HTMLSelectElement>("linkedinAccount");
let linkedinAccountRevision = 0;
const linkedinAutomaticDelivery = input("linkedinAutomaticDelivery");
const linkedinCompany = element<HTMLSelectElement>("linkedinLiveCompany");
const linkedinSubmit = button("scheduleLinkedinLive");
const linkedinStatus = element("linkedinLiveStatus");
const linkedinPost = element<HTMLAnchorElement>("linkedinLivePost");
const linkedinSeriesLabel = element<HTMLLabelElement>("linkedinSeriesLabel");
const linkedinSeries = input("linkedinSeries");
const linkedinSeriesSkip = button("linkedinSeriesSkip");
const linkedinSeriesStop = button("linkedinSeriesStop");
const existingLinkedinList = element<HTMLSelectElement>("existingLinkedinList");
const existingLinkedinMore = button("existingLinkedinMore");
const existingLinkedinChoose = button("existingLinkedinChoose");
let existingLinkedinNext: number | null = null;
const linkedinProvider = () => linkedinAccount.value === "member" ? "linkedin-member" : "linkedin";

function resetLinkedinAccount() {
  linkedinAccountRevision++;
  linkedinAutomaticDelivery.checked = true;
  linkedinCompany.replaceChildren();
  linkedinSubmit.disabled = true;
  existingLinkedinList.replaceChildren();
  existingLinkedinNext = null;
  existingLinkedinMore.hidden = true;
  existingLinkedinChoose.disabled = true;
  linkedinStatus.textContent = "";
  linkedinPost.hidden = true;
  linkedinPost.removeAttribute("href");
  linkedinSeriesLabel.hidden = !active || active.event.event.schedule.repeat === "none";
  linkedinSeries.checked = true;
  linkedinSeriesSkip.hidden = true;
  linkedinSeriesStop.hidden = true;
  button("prepareLinkedinLive").disabled = false;
  button("loadLinkedinCompany").textContent = linkedinAccount.value === "member"
    ? "Load LinkedIn profile" : "Load LinkedIn Company Page";
}

linkedinAccount.onchange = resetLinkedinAccount;
const encoder = element("encoderSettings");
const key = input("encoderKey");
const backupKey = input("backupEncoderKey");
const backupEnabled = input("enableBackupEncoder");
const outputHeight = element<HTMLSelectElement>("backupOutputHeight");
backupEnabled.onchange = () => { element("backupOutput").hidden = !backupEnabled.checked; };
const srtBackupEnabled = input("enableSrtBackup");
const srtBackupSecret = input("srtBackupPassphrase");
const srtBackupHeight = element<HTMLSelectElement>("srtBackupHeight");
srtBackupEnabled.onchange = () => { element("srtBackupOptions").hidden = !srtBackupEnabled.checked; };
const previewEncoder = input("privateEncoderPreview");
const encoderPreviewPanel = element("encoderPreview");
const encoderPreviewStatus = element("encoderPreviewStatus");
const encoderPreviewVideo = element<HTMLVideoElement>("encoderPreviewVideo");
const releasePreview = button("releaseEncoderPreview");
const PreviewHls = (window as Window & {Hls?: PreviewHlsFactory}).Hls;
let previewPlayer: PreviewHls | undefined;
let previewOpened = 0;
let checkingPreview = false;
const starting = input("calendarFirst"), ending = input("calendarLast");
const programPanel = element<HTMLDetailsElement>("eventProgramPanel");
const programChoice = element<HTMLSelectElement>("programChoice");
const chatPanel = element<HTMLDetailsElement>("operatorChat");
const chatForm = element<HTMLFormElement>("eventChatSettings");
const chatEnabled = chatForm.elements.namedItem("enabled") as HTMLInputElement;
const chatInterval = chatForm.elements.namedItem("interval") as HTMLInputElement;
const chatMessages = element<HTMLOListElement>("operatorChatMessages");
const chatStatus = element("operatorChatStatus");
const root = "/api/live-events";
const publicBase = document.body.dataset.publicUrl || location.origin;

let calendar: Calendar;
let active: {event: LiveEvent; instance: Occurrence} | undefined;
let editing: LiveEvent | undefined;
let registrationEvent: string | undefined;
let pending = false;
let program: EventProgram | undefined;
let programSources: ProgramSource[] = [];
credential.value = sessionStorage.getItem("cueporchOperator") || "";

async function request<T>(path: string, method = "GET", body?: object, integration = false): Promise<T> {
  const headers: Record<string, string> = {Authorization: "Bearer " + credential.value};
  const multipart = body instanceof FormData;
  if (body && !multipart) headers["Content-Type"] = "application/json";
  if (integration) headers["X-CuePorch-Integration"] = "1";
  const result = await fetch(path, {method, headers, body: multipart ? body : body ? JSON.stringify(body) : undefined});
  if (!result.ok) {
    const detail = new DOMParser().parseFromString(await result.text(), "text/html").querySelector("p")?.textContent;
    throw new Error(detail || `Request failed (${result.status})`);
  }
  return result.status === 204 ? undefined as T : result.json();
}

setupLiveApi(request);
const chooseEmailEvent = setupEventMail(request);
const chooseCsvOccurrence = setupAttendeeCsv(request);
const chooseRosterOccurrence = setupEventRoster(request, downloadEventFile);
const chooseInterpreterOccurrence = interpreterControls(request);

async function action(work: () => Promise<void>, message = "") {
  if (pending) return;
  pending = true;
  status.textContent = "";
  workspace.inert = true;
  try {
    await work();
    status.textContent = message;
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    pending = false;
    workspace.inert = false;
  }
}

function mediaPath(suffix: string) {
  return `${root}/${active!.event.id}/${active!.instance.id}/${suffix}`;
}

function stopEncoderPreview() {
  previewPlayer?.destroy();
  previewPlayer = undefined;
  if (previewOpened) {
    encoderPreviewVideo.removeAttribute("src");
    encoderPreviewVideo.load();
  }
  previewOpened = 0;
  encoderPreviewVideo.hidden = true;
}

function showEncoderPreview(open: boolean) {
  stopEncoderPreview();
  encoderPreviewPanel.hidden = !open;
  releasePreview.disabled = true;
  encoderPreviewStatus.textContent = open ? "Waiting for the private encoder signal." : "";
  if (open) void refreshEncoderPreview();
}

async function refreshEncoderPreview() {
  if (!active || checkingPreview || encoderPreviewPanel.hidden) return;
  const selection = active;
  checkingPreview = true;
  try {
    const result = await request<EncoderPreview>(mediaPath("preview"));
    if (selection !== active || encoderPreviewPanel.hidden) return;
    releasePreview.disabled = result.state !== "signal";
    encoderPreviewStatus.textContent = {
      waiting: "Waiting for the private encoder signal.", signal: "The private encoder signal is ready.",
      ended: "The private encoder signal has ended.", inactive: "This occurrence is no longer in preview.",
      released: "The encoder is live.",
    }[result.state];
    if (!result.address) stopEncoderPreview();
    else if (Date.now() - previewOpened > 25 * 60 * 1000) {
      stopEncoderPreview();
      if (PreviewHls?.isSupported()) {
        previewPlayer = new PreviewHls({enableWorker: true, liveDurationInfinity: true});
        previewPlayer.loadSource(result.address);
        previewPlayer.attachMedia(encoderPreviewVideo);
      } else if (encoderPreviewVideo.canPlayType("application/vnd.apple.mpegurl")) {
        encoderPreviewVideo.src = result.address;
      } else {
        encoderPreviewStatus.textContent = "This browser cannot play the private preview.";
        return;
      }
      previewOpened = Date.now();
      encoderPreviewVideo.hidden = false;
    }
  } catch (error) {
    if (selection === active && !encoderPreviewPanel.hidden) {
      releasePreview.disabled = true;
      encoderPreviewStatus.textContent = error instanceof Error ? error.message : "Could not check the private encoder signal.";
    }
  } finally {
    checkingPreview = false;
  }
}

window.setInterval(() => { if (!document.hidden) void refreshEncoderPreview(); }, 3000);

async function downloadEventFile(path: string, filename: string) {
  const response = await fetch(path, {headers: {Authorization: "Bearer " + credential.value}});
  if (!response.ok) throw new Error("Could not download the event report.");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

const {close: closeEngagement} = setupProducerEngagement(() => mediaPath("engagement"), request, (feature, filename) =>
  action(() => downloadEventFile(mediaPath(`engagement/${feature}/export`), filename)),
);

const registrationEditor = element<HTMLFormElement>("registrationEditor");
const openRegistration = setupRegistration(
  (suffix, method, body) => request(root + "/" + registrationEvent + suffix, method, body),
  () => downloadEventFile(root + "/" + registrationEvent + "/registration/export", "cueporch-event-registrants.csv"),
  () => credential.value,
);

function choose(event: LiveEvent, instance: Occurrence) {
  if (registrationEvent !== event.id) {
    registrationEditor.hidden = true;
    registrationEvent = undefined;
  }
  active = {event, instance};
  chooseEmailEvent(event.id);
  chooseCsvOccurrence(event.id, instance.id);
  chooseRosterOccurrence(event.id, instance.id);
  closeEngagement();
  chatPanel.open = false;
  chatMessages.replaceChildren();
  delete chatMessages.dataset.revision;
  programPanel.open = false;
  program = undefined;
  programSources = [];
  renderProgram();
  output.hidden = false;
  encoder.hidden = true;
  key.value = "";
  backupKey.value = "";
  element("backupEncoderSettings").hidden = true;
  element("dashSettings").hidden = true;
  input("dashUploadToken").value = "";
  element("srtSettings").hidden = true;
  input("srtPassphrase").value = "";
  srtBackupSecret.value = "";
  element("srtBackupCredentials").hidden = true;
  element("srtBackupChoice").hidden = event.event.latency !== "low";
  srtBackupEnabled.checked = false;
  element("srtBackupOptions").hidden = true;
  srtBackupHeight.value = "720";
  previewEncoder.checked = event.preview_encoder;
  showEncoderPreview(instance.delivery === "preview");
  health.textContent = instance.delivery === "ready" ? "Encoder is ready." : instance.delivery.replaceAll("_", " ");
  measurements.textContent = "";
  analytics.open = false;
  analyticsRows.replaceChildren();
  analyticsStatus.textContent = "";
  captionSettings.open = false;
  captionStatus.textContent = "";
  replayCaptions.open = false;
  replayCaptionForm.reset();
  replayCaptionStatus.textContent = replayCaptionRevision = "";
  button("downloadReplayCaptions").disabled = button("saveReplayCaptions").disabled = true;
  libraryRecording.open = false;
  libraryRecordingStatus.textContent = "";
  libraryRecordingLink.hidden = saveLibraryRecording.disabled = true;
  eventPlaylistPanel.open = false;
  eventPlaylistPanel.hidden = event.event.schedule.repeat === "none";
  eventPlaylistForm.reset();
  eventPlaylistStatus.textContent = "";
  button("saveEventPlaylist").disabled = true;
  eventPlayerPanel.open = false;
  clearPlayerOptions();
  destinations.open = false;
  destinationForm.reset();
  destinationList.replaceChildren();
  destinationStatus.textContent = "";
  youtubePanel.hidden = true;
  youtubeList.replaceChildren();
  youtubeNext = null;
  youtubeMore.hidden = true;
  youtubeStatus.textContent = "";
  youtubeSubmit.disabled = true;
  youtubeAddSeriesLabel.hidden = event.event.schedule.repeat === "none";
  youtubeAddSeries.checked = false;
  youtubeCreate.reset();
  youtubeCreateButton.textContent = "Create YouTube broadcast";
  youtubeSeriesLabel.hidden = event.event.schedule.repeat === "none";
  youtubeSeriesInput.disabled = youtubeSeriesLabel.hidden;
  youtubeSeriesSkip.hidden = true;
  youtubeSeriesStop.hidden = true;
  facebookForm.hidden = true;
  facebookTarget.value = "page";
  facebookPrivacyLabel.hidden = true;
  facebookPrivacy.value = "";
  facebookPageLabel.textContent = "Facebook Live Page";
  facebookLoad.textContent = "Load Facebook Live Pages";
  facebookPage.replaceChildren();
  facebookSubmit.disabled = true;
  facebookAutomatic.checked = true;
  facebookCancel.disabled = true;
  facebookRestoreDate.hidden = true;
  facebookSeriesControls.hidden = event.event.schedule.repeat === "none";
  facebookSeries.checked = !facebookSeriesControls.hidden;
  facebookSeries.disabled = false;
  facebookSeriesStop.disabled = true;
  facebookStatusRevision++;
  facebookStatus.textContent = "";
  linkedinForm.hidden = true;
  linkedinAccount.value = "company";
  resetLinkedinAccount();
  button("editEvent").disabled = !event.editable;
  button("prepareEncoder").disabled = !calendar.host_ready || ["withdrawing", "withdrawn", "replay"].includes(instance.delivery);
  button("prepareSrt").disabled = button("prepareEncoder").disabled;
  const rtmpAvailable = calendar.direct_rtmp && event.event.latency === "low";
  chooseInterpreterOccurrence(rtmpAvailable && !["withdrawn", "withdrawing", "replay"].includes(instance.delivery)
    ? mediaPath("interpreters") : null);
  button("prepareLowRtmp").hidden = button("stopLowRtmp").hidden = !rtmpAvailable;
  element("backupEncoderOptions").hidden = !rtmpAvailable;
  backupEnabled.checked = false;
  element("backupOutput").hidden = true;
  outputHeight.value = "720";
  button("prepareLowRtmp").disabled = button("prepareEncoder").disabled;
  button("prepareDash").hidden = button("stopDash").hidden = event.event.latency !== "fail-safe";
  button("prepareDash").disabled = button("prepareEncoder").disabled;
  button("withdrawEvent").textContent = instance.delivery === "scheduled" ? "Cancel this occurrence" : "Withdraw this occurrence";
  button("withdrawEvent").disabled = instance.delivery === "withdrawn" ||
    (instance.delivery !== "scheduled" && !calendar.host_ready);
  element("productionTitle").textContent = `${event.event.title} · ${instance.local.replace("T", " ")} ${event.event.schedule.timezone}`;
  const preference = event.event.setup?.method;
  element("productionPreference").textContent = preference ?
    `Preferred streaming method: ${preference === "encoder" ? "External encoder" : "Browser studio"}` : "";
  const watch = element<HTMLAnchorElement>("eventWatch");
  const studio = element<HTMLAnchorElement>("eventStudio");
  studio.href = `/operator/live-events/${event.id}/${instance.id}/studio`;
  element<HTMLAnchorElement>("eventRecordOnly").href = studio.href + "?mode=record";
  element("eventShare").hidden = !event.watch;
  element("eventEmbedField").hidden = !event.embed;
  function occurrenceLink(path: string) {
    const address = new URL(path, publicBase);
    address.searchParams.set("occurrence", String(instance.id));
    return address.href;
  }
  if (event.watch) watch.href = occurrenceLink(event.watch);
  const address = event.embed ? occurrenceLink(event.embed) : "";
  input("eventEmbedCode").value = address ? `<iframe src="${address.replaceAll("&", "&amp;")}" title="Live event" allowfullscreen></iframe>` : "";
}

function releaseArtworkPreview(name: ArtworkField) {
  if (artworkPreview[name]) URL.revokeObjectURL(artworkPreview[name]);
  artworkPreview[name] = "";
}

function clearPlayerOptions() {
  playerOptions = undefined;
  removedArtwork.clear();
  for (const name of artworkFields) {
    releaseArtworkPreview(name);
    playerArtwork[name].disabled = true;
  }
  eventPlayerForm.reset();
  appearanceFields.disabled = true;
  eventPlayerStatus.textContent = "";
  button("saveEventPlayer").disabled = true;
  eventLogo.hidden = true;
  eventLogo.removeAttribute("src");
  previewWaiting(null);
}

function paintWaitingPreview() {
  if (!active || !playerOptions) return;
  const {event, instance} = active;
  const next = playerOption("show_schedule").checked;
  const recurring = playerOption("show_recurring").checked ? playerOptions.recurring : "";
  const poster = artworkPreview.poster || (!removedArtwork.has("poster") ? playerOptions.image : null);
  const logo = artworkPreview.logo || (!removedArtwork.has("logo") ? playerOptions.logo_image : null);
  playerArtwork.poster.disabled = !poster;
  playerArtwork.logo.disabled = !logo;
  eventLogo.hidden = !logo;
  if (logo) eventLogo.src = logo;
  else eventLogo.removeAttribute("src");
  previewWaiting(next || recurring || poster ? {
    poster, date: next ? instance.start : "", recurring,
    message: next ? `${instance.local.replace("T", " ")} (${event.event.schedule.timezone})` : "",
  } : null);
}

function showPlayerOptions(options: EventPlayerOptions) {
  playerOptions = options;
  removedArtwork.clear();
  for (const name of artworkFields) releaseArtworkPreview(name);
  eventPlayerForm.reset();
  for (const key of displaySwitches) playerOption(key).checked = options[key];
  playerOption("byline").value = options.byline;
  for (const key of playerSwitches) playerOption(key).checked = options[key];
  for (const key of playerColors) playerOption(key).value = options[key];
  playPosition.value = options.play_position;
  appearanceFields.disabled = !options.customize;
  playerOption("show_recurring").disabled = !options.recurring;
  button("saveEventPlayer").disabled = false;
  paintWaitingPreview();
}

eventPlayerPanel.ontoggle = () => {
  if (eventPlayerPanel.open && active && !playerOptions) {
    void action(async () => showPlayerOptions(await request<EventPlayerOptions>(`${root}/${active!.event.id}/player`)));
  }
};
eventPlayerForm.oninput = (event) => {
  if (event.target === playerOption("customize")) appearanceFields.disabled = !playerOption("customize").checked;
  for (const name of artworkFields) {
    if (event.target !== playerOption(name)) continue;
    releaseArtworkPreview(name);
    const file = playerOption(name).files?.[0];
    if (file) artworkPreview[name] = URL.createObjectURL(file);
  }
  paintWaitingPreview();
};
for (const name of artworkFields) {
  playerArtwork[name].onclick = () => {
    removedArtwork.add(name);
    playerOption(name).value = "";
    releaseArtworkPreview(name);
    paintWaitingPreview();
  };
}
eventPlayerForm.onsubmit = (event) => {
  event.preventDefault();
  void action(async () => {
    if (!active || !playerOptions) return;
    const eventId = active.event.id;
    const fields = {
      byline: playerOption("byline").value,
      ...Object.fromEntries(displaySwitches.map(key => [key, playerOption(key).checked])),
      ...Object.fromEntries(playerSwitches.map(key => [key, playerOption(key).checked])),
      ...Object.fromEntries(playerColors.map(key => [key, playerOption(key).value])),
      play_position: playPosition.value,
    };
    const images = {
      poster: removedArtwork.has("poster") ? null : playerOptions.poster,
      logo: removedArtwork.has("logo") ? null : playerOptions.logo,
    };
    const files = artworkFields.map(name => ({name, file: playerOption(name).files?.[0]}));
    for (const {name, file} of files) {
      if (!file) continue;
      const upload = new FormData();
      upload.append("logo", file);
      images[name] = (await request<{logo: string}>("/api/review-logos", "POST", upload)).logo;
    }
    const options = await request<EventPlayerOptions>(`${root}/${eventId}/player`, "PUT", {...fields, ...images});
    if (active?.event.id !== eventId) return;
    showPlayerOptions(options);
    eventPlayerStatus.textContent = "Event player saved.";
  });
};

function readCalendar(value: Calendar) {
  calendar = value;
  element("eventsHost").hidden = value.host_ready;
  const priorPreset = preset.value;
  preset.replaceChildren(new Option("Choose an earlier event", ""), ...value.events.map((entry) => new Option(entry.event.title, entry.id)));
  preset.value = value.events.some((entry) => entry.id === priorPreset) ? priorPreset : "";
  const rows = element<HTMLTableSectionElement>("eventCalendarRows");
  rows.replaceChildren();
  const sorted = value.events.flatMap((event) => event.occurrences.map((instance) => ({event, instance})))
    .sort((a, b) => a.instance.id - b.instance.id);
  for (const {event, instance} of sorted) {
    const row = rows.insertRow();
    row.insertCell().textContent = event.event.title;
    const timestamp = document.createElement("time");
    timestamp.dateTime = instance.start;
    timestamp.textContent = instance.local.replace("T", " ") + " " + event.event.schedule.timezone;
    row.insertCell().append(timestamp);
    row.insertCell().textContent = event.publication === "waiting" ? "Waiting for publication approval" :
      instance.program ? "Program " + instance.program.replaceAll("_", " ") : instance.delivery.replaceAll("_", " ");
    row.insertCell().textContent = value.overlaps.includes(event.id) ? "Overlapping time" : "";
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = "Open production";
    control.setAttribute("aria-label", `Open production for ${event.event.title} at ${instance.local.replace("T", " ")}`);
    control.onclick = () => choose(event, instance);
    row.insertCell().append(control);
    if (active?.event.id === event.id && active.instance.id === instance.id) choose(event, instance);
  }
}

const entry = new URL(location.href).searchParams;
let startNew = entry.get("create") === "1";
let activityEvent = entry.get("event");

async function load() {
  const query = new URLSearchParams();
  if (starting.value) query.set("from", starting.value);
  if (ending.value) query.set("to", ending.value);
  const calendar = await request<Calendar>(root + "?" + query);
  readCalendar(calendar);
  sessionStorage.setItem("cueporchOperator", credential.value);
  identify.hidden = true;
  workspace.hidden = false;
  if (startNew) {
    startNew = false;
    openEditor();
  } else if (activityEvent) {
    const match = calendar.events.find(event => event.id === activityEvent);
    activityEvent = null;
    if (match) openEditor(match);
    else throw new Error("This event is unavailable.");
  }
}

identify.onsubmit = (event) => { event.preventDefault(); void action(load); };
if (credential.value) void action(load);
button("calendarRefresh").onclick = () => void action(load);
function openEditor(current?: LiveEvent) {
  editing = current;
  form.reset();
  element("eventEditorHeading").textContent = current ? "Edit event" : "New event";
  element("eventSetupMode").hidden = Boolean(current);
  element("eventReuse").hidden = true;
  element("eventStartingOptions").hidden = Boolean(current);
  preset.value = "";
  preset.required = false;
  field("slug").readOnly = Boolean(current);
  const values = current?.event;
  if (values) {
    for (const name of ["title", "description", "type", "visibility", "latency"] as const) field(name).value = values[name];
    field("save_replay").checked = values.save_replay;
    field("archive_replay").checked = Boolean(values.archive_replay);
    field("dvr_seconds").value = values.dvr_seconds?.toString() ?? "";
    const schedule = values.schedule;
    for (const name of ["timezone", "start", "end", "repeat"] as const) field(name).value = schedule[name];
    field("interval").value = String(schedule.interval);
    field("count").value = schedule.count ? String(schedule.count) : "";
    field("until").value = schedule.until || "";
    for (const weekday of form.querySelectorAll<HTMLInputElement>('input[name="weekday"]')) {
      weekday.checked = schedule.weekdays.includes(Number(weekday.value));
    }
    field("slug").value = current!.slug;
    field("method").value = values.setup?.method ?? "browser";
  } else {
    field("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  }
  showEmbedPolicy(values?.embed_policy);
  showRegionalPolicy(values?.regional_policy);
  showClassification(values?.content_rating);
  syncViewingPassword();
  recurrence();
  syncArchiveOption();
  form.hidden = false;
  field("title").focus();
}

function recurrence() {
  element("recurringFields").hidden = field("repeat").value === "none";
  element("eventWeekdays").hidden = field("repeat").value !== "weeks";
}

function syncArchiveOption() {
  field("archive_replay").disabled = !field("save_replay").checked;
}

function syncEmbedPolicy() {
  const domains = field("embed_mode").value === "domains";
  element("eventEmbedDomains").hidden = !domains;
  field("embed_domains").required = domains;
}

function showEmbedPolicy(policy?: EventSettings["embed_policy"]) {
  field("embed_mode").value = policy?.mode ?? "anywhere";
  field("embed_domains").value = policy?.domains.join("\n") ?? "";
  syncEmbedPolicy();
}

function syncViewingPassword() {
  const protectedEvent = field("visibility").value === "password";
  element("eventViewingPassword").hidden = !protectedEvent;
  field("view_password").required = protectedEvent && editing?.event.visibility !== "password";
  if (!protectedEvent) field("view_password").value = "";
}

function syncRegionalPolicy() {
  const active = field("region_mode").value !== "anywhere";
  element("eventRegionFields").hidden = !active;
  field("region_areas").required = active;
}

function showRegionalPolicy(policy?: RegionalPolicy) {
  field("region_mode").value = policy?.mode ?? "anywhere";
  field("region_areas").value = policy?.areas.join("\n") ?? "";
  syncRegionalPolicy();
  button("exportEventRegions").hidden = !editing || (editing.event.regional_policy?.mode ?? "anywhere") === "anywhere";
}

function syncContentDescriptors() {
  const mature = field("rating").value === "mature";
  const options = element<HTMLFieldSetElement>("eventContentDescriptors");
  options.hidden = options.disabled = !mature;
}

function showClassification(values?: Classification) {
  field("rating").value = values?.rating ?? "unrated";
  field("advertisement").checked = !!values?.advertisement;
  field("realistic_ai").checked = !!values?.realistic_ai;
  for (const item of form.querySelectorAll<HTMLInputElement>('input[name="content_descriptor"]')) {
    item.checked = values?.content.includes(item.value) ?? false;
  }
  syncContentDescriptors();
}

button("newEvent").onclick = () => openEditor();
button("editEvent").onclick = () => openEditor(active?.event);
button("configureEventRegistration").onclick = () => void action(async () => {
  if (active) {
    registrationEvent = active.event.id;
    await openRegistration();
  }
});
button("closeEventEditor").onclick = () => { form.hidden = true; };
field("repeat").onchange = recurrence;
field("save_replay").onchange = syncArchiveOption;
field("embed_mode").onchange = syncEmbedPolicy;
field("visibility").onchange = syncViewingPassword;
field("region_mode").onchange = syncRegionalPolicy;
field("rating").onchange = syncContentDescriptors;
const regionsFile = input("importEventRegions");
regionsFile.onchange = () => void action(async () => {
  const file = regionsFile.files?.[0];
  if (!file) return;
  if (file.size > 262144) throw new Error("Choose a UTF-8 territory CSV under 256 KiB.");
  const imported = await request<RegionalPolicy>(root + "/regions/import", "POST", {csv: await file.text()});
  showRegionalPolicy(imported);
  regionsFile.value = "";
}, "Locations loaded into event settings.");
button("exportEventRegions").onclick = () => void action(async () => {
  if (!editing) return;
  const response = await fetch(`${root}/${editing.id}/regions/export`, {
    headers: {Authorization: "Bearer " + credential.value},
  });
  if (!response.ok) throw new Error("Could not download the saved locations.");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = "cueporch-event-regions.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
function showStartingOptions(options: StartingOptions) {
  field("method").value = options.method;
  field("registration").checked = options.registration;
  field("preview_encoder").checked = options.preview_encoder;
  field("chat").checked = options.chat.enabled;
  field("chat_interval").value = String(options.chat.interval);
  field("captions").checked = options.captions.enabled;
  field("caption_language").value = options.captions.language;
  field("qa").checked = options.qa.enabled;
  field("qa_anonymous").checked = options.qa.anonymous;
  field("qa_review").checked = options.qa.review;
}

function showTypeDefaults() {
  const definition = typeDefaults[field("type").value];
  field("latency").value = definition.latency;
  showStartingOptions({...startingOptions, method: definition.method ?? "browser",
    registration: !!definition.registration, chat: {...startingOptions.chat, enabled: !!definition.chat},
    captions: {...startingOptions.captions, enabled: !!definition.captions},
    qa: {...startingOptions.qa, enabled: !!definition.qa}});
}

mode.onchange = () => {
  const reuse = mode.value === "reuse";
  element("eventReuse").hidden = !reuse;
  preset.required = reuse;
  preset.value = "";
  if (mode.value === "type") showTypeDefaults();
  else {
    field("latency").value = "standard";
    showStartingOptions(startingOptions);
  }
};
field("type").onchange = () => { if (!editing && mode.value === "type") showTypeDefaults(); };
preset.onchange = () => void action(async () => {
  const prior = calendar.events.find((entry) => entry.id === preset.value);
  if (!prior || mode.value !== "reuse") return;
  const options = await request<StartingOptions>(`${root}/${prior.id}/setup`);
  const value = prior.event;
  for (const name of ["title", "type", "description", "latency"] as const) field(name).value = value[name];
  field("visibility").value = "private";
  syncViewingPassword();
  field("save_replay").checked = value.save_replay;
  field("archive_replay").checked = value.archive_replay ?? true;
  syncArchiveOption();
  field("dvr_seconds").value = value.dvr_seconds?.toString() ?? "";
  field("timezone").value = value.schedule.timezone;
  showEmbedPolicy(value.embed_policy);
  showRegionalPolicy(value.regional_policy);
  showClassification(value.content_rating);
  showStartingOptions(options);
}, "Event settings loaded.");

form.onsubmit = (event) => {
  event.preventDefault();
  void action(async () => {
    const read = new FormData(form);
    const repeat = field("repeat").value as Repeat;
    const schedule: Schedule = {timezone: field("timezone").value, start: field("start").value, end: field("end").value,
      repeat, interval: Number(field("interval").value || 1), weekdays: read.getAll("weekday").map(Number),
      until: repeat !== "none" && field("until").value || null, count: repeat !== "none" && Number(field("count").value) || null};
    const target = editing ? root + "/" + editing.id : root;
    const method = editing ? "PUT" : "POST";
    const setup = editing ? undefined : {
      method: field("method").value, registration: field("registration").checked,
      preview_encoder: field("preview_encoder").checked,
      chat: {enabled: field("chat").checked, interval: Number(field("chat_interval").value)},
      captions: {enabled: field("captions").checked, language: field("caption_language").value},
      qa: {enabled: field("qa").checked, anonymous: field("qa_anonymous").checked, review: field("qa_review").checked},
    };
    const created = await request<LiveEvent>(target, method, {
      revision: editing?.revision,
      preferred_method: editing && field("method").value !== (editing.event.setup?.method ?? "browser") ? field("method").value : undefined,
      starting_options: setup, reuse_event: !editing && mode.value === "reuse" ? preset.value : undefined,
      slug: field("slug").value, title: field("title").value, description: field("description").value,
      type: field("type").value, visibility: field("visibility").value, latency: field("latency").value,
      view_password: field("view_password").value || undefined,
      embed_policy: {mode: field("embed_mode").value,
        domains: field("embed_domains").value.split("\n").map((domain) => domain.trim()).filter(Boolean)},
      regional_policy: {mode: field("region_mode").value,
        areas: field("region_areas").value.split("\n").map((region) => region.trim()).filter(Boolean)},
      content_rating: {rating: field("rating").value, content: read.getAll("content_descriptor"),
        advertisement: field("advertisement").checked, realistic_ai: field("realistic_ai").checked},
      save_replay: field("save_replay").checked,
      archive_replay: field("archive_replay").checked,
      dvr_seconds: field("dvr_seconds").value === "" ? null : field("dvr_seconds").valueAsNumber, schedule,
    });
    form.hidden = true;
    await load();
    if (created.occurrences.length) choose(created, created.occurrences[0]);
  }, "Event saved.");
};

previewEncoder.onchange = () => void action(async () => {
  const event = active!.event;
  try {
    const value = await request<{preview: boolean}>(`${root}/${event.id}/encoder-settings`, "PUT", {preview: previewEncoder.checked});
    event.preview_encoder = value.preview;
  } finally {
    previewEncoder.checked = event.preview_encoder;
  }
}, "Encoder preview preference saved.");
function showEncoder(connection: Encoder) {
  input("rtmpServer").value = connection.server;
  input("rtmpsServer").value = connection.secure_server || "";
  element("secureEncoder").hidden = !connection.secure_server;
  key.value = connection.key;
  backupKey.value = connection.backup_key || "";
  element("backupEncoderSettings").hidden = !connection.backup_key;
  encoder.hidden = false;
  button("withdrawEvent").textContent = "Withdraw this occurrence";
  button("withdrawEvent").disabled = false;
  showEncoderPreview(connection.preview);
  health.textContent = connection.preview ? "Encoder prepared for private preview." : "Waiting for the encoder.";
}
button("prepareEncoder").onclick = () => void action(async () => {
  showEncoder(await request<Encoder>(mediaPath("encoder"), "POST"));
});
button("prepareLowRtmp").onclick = () => void action(async () => {
  showEncoder(await request<Encoder>(mediaPath("rtmp"), "POST",
    {backup: backupEnabled.checked, height: Number(outputHeight.value)}));
  chooseInterpreterOccurrence(mediaPath("interpreters"));
}, "Low-latency RTMP is ready for the encoder.");
button("stopLowRtmp").onclick = () => void action(async () => {
  await request(mediaPath("rtmp"), "DELETE");
  chooseInterpreterOccurrence(null);
  encoder.hidden = true;
  key.value = "";
  backupKey.value = "";
}, "Low-latency RTMP stopped.");
button("copyBackupEncoderKey").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(backupKey.value);
}, "Backup encoder key copied.");
button("prepareDash").onclick = () => void action(async () => {
  const connection = await request<{manifest: string; token: string; buffer_seconds: number}>(mediaPath("dash"), "POST");
  input("dashManifest").value = connection.manifest;
  input("dashUploadToken").value = connection.token;
  element("dashSettings").hidden = false;
  button("withdrawEvent").textContent = "Withdraw this occurrence";
  button("withdrawEvent").disabled = false;
  health.textContent = `Waiting for a DASH contribution. Playback buffers ${connection.buffer_seconds} seconds.`;
});
button("stopDash").onclick = () => void action(async () => {
  await request(mediaPath("dash"), "DELETE");
  element("dashSettings").hidden = true;
  input("dashUploadToken").value = "";
}, "MPEG-DASH contribution stopped.");
button("copyDashToken").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(input("dashUploadToken").value);
}, "DASH upload token copied.");
releasePreview.onclick = () => void action(async () => {
  await request<EncoderPreview>(mediaPath("preview"), "POST");
  showEncoderPreview(false);
  active!.instance.delivery = "ready";
  health.textContent = "Released. Waiting for the audience stream to start.";
}, "Private encoder released.");
button("copyEncoderKey").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(key.value);
}, "Stream key copied.");
button("prepareSrt").onclick = () => void action(async () => {
  const result = await request<SrtListener>(mediaPath("srt"), "POST", {
    interface: input("srtInterface").value, port: Number(input("srtPort").value),
    backup_port: srtBackupEnabled.checked ? Number(input("srtBackupPort").value) : undefined,
    height: srtBackupEnabled.checked ? Number(srtBackupHeight.value) : undefined,
  });
  input("srtServer").value = result.server;
  input("srtPassphrase").value = result.passphrase;
  input("srtBackupServer").value = result.backup_server || "";
  srtBackupSecret.value = result.backup_passphrase || "";
  element("srtBackupCredentials").hidden = !result.backup_passphrase;
  element("srtSettings").hidden = false;
  button("withdrawEvent").textContent = "Withdraw this occurrence";
  button("withdrawEvent").disabled = false;
  if (result.preview) showEncoderPreview(true);
}, "SRT listener is ready for the encoder.");
button("stopSrt").onclick = () => void action(async () => {
  await request(mediaPath("srt"), "DELETE");
  element("srtSettings").hidden = true;
  input("srtPassphrase").value = "";
  srtBackupSecret.value = "";
}, "SRT listener stopped.");
button("copySrtPassphrase").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(input("srtPassphrase").value);
}, "SRT passphrase copied.");
button("copySrtBackupPassphrase").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(srtBackupSecret.value);
}, "Backup SRT passphrase copied.");

async function refreshDestinations(result?: Destinations) {
  const selection = active;
  result = result || await request<Destinations>(mediaPath("destinations"));
  if (selection !== active) return;
  destinationForm.hidden = !result.available;
  youtubePanel.hidden = !result.available;
  facebookForm.hidden = !result.available;
  linkedinForm.hidden = !result.available;
  destinationStatus.textContent = result.available ? "" : "Destinations require an event open to the public without registration.";
  const labels: Record<string, string> = {
    waiting: "Waiting for live signal", connecting: "Connecting", sending: "Sending",
    retrying: "Reconnecting", disabled: "Off", disconnected: "Disconnected",
    bound: "Live on LinkedIn", closing: "Ending on LinkedIn", ended: "Ended",
    end_unconfirmed: "End unconfirmed on LinkedIn",
  };
  const presetLabels: Record<string, string> = {
    instagram: "Instagram Live", tiktok: "TikTok LIVE", x: "X Live", linkedin: "LinkedIn Live",
    "vimeo-ott": "Vimeo OTT",
  };
  destinationList.replaceChildren(...result.destinations.map(item => {
    const row = document.createElement("li");
    const linkedin = item.provider === "linkedin-live" || item.provider === "linkedin-member-live";
    const scope = item.provider === "youtube-live" ? " · YouTube Live for this date"
      : item.provider === "facebook-live" ? " · Facebook Live for this date"
      : item.provider === "facebook-profile-live" ? " · Facebook Profile for this date"
      : linkedin ? " · LinkedIn Live for this date"
      : item.opens !== null ? ` · ${presetLabels[item.preset || ""] || "Patreon Live"} for this date` : "";
    const state = item.provider_phase && item.provider_phase !== "registered" && item.state !== "disconnected"
      ? item.provider_phase : item.state;
    const reusable = item.reusable;
    const customSeries = !reusable && !item.provider && item.opens === null && !!active && active.event.event.schedule.repeat !== "none";
    const enabled = customSeries ? item.configured_enabled : item.enabled;
    row.textContent = `${item.name}${reusable ? " · Reusable" : ""}${reusable ? ` · This date ${item.enabled ? "on" : "off"}` : ""}${scope} · ${item.server} · ${labels[state] || state}`;
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = enabled ? "Turn off" : "Turn on";
    control.setAttribute("aria-label", `Turn ${enabled ? "off" : "on"} ${item.name}`);
    control.disabled = item.state === "disconnected"
      || (!enabled && !result!.available);
    control.onclick = () => void action(async () => {
      const update = await request<{state: string} | undefined>(
        mediaPath(`destinations/${item.id}`), "PUT", {enabled: !enabled});
      await refreshDestinations();
      if (linkedin && update?.state === "pending") {
        destinationStatus.textContent = "LinkedIn is still confirming delivery. Select Turn on again to check.";
      } else if (linkedin && update?.state === "unconfirmed") {
        destinationStatus.textContent = "LinkedIn did not confirm setup. Check the broadcast on LinkedIn.";
      }
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = reusable ? "Stop reusing key" : "Remove";
    remove.setAttribute("aria-label", reusable ? `Stop reusing key for ${item.name}` : `Remove ${item.name}`);
    remove.onclick = () => void action(async () => {
      await request(mediaPath(`destinations/${item.id}`), "DELETE");
      await refreshDestinations();
    });
    row.append(control, remove);
    if (customSeries) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      const text = item.skipped ? "Use this date" : "Skip this date";
      toggle.textContent = text;
      toggle.setAttribute("aria-label", `${text} for ${item.name}`);
      toggle.onclick = () => void action(async () => {
        await request(mediaPath(`destinations/${item.id}`), "PUT", {enabled: item.skipped, only_date: true});
        await refreshDestinations();
      });
      row.append(toggle);
    }
    return row;
  }));
}

async function refreshYouTubeSeries() {
  if (!active) return;
  const selection = active;
  try {
    const result = await request<{ active: boolean; inherited: boolean; skipped: boolean; restorable?: boolean }>(mediaPath("destinations/youtube/series"));
    if (selection !== active) return;
    const restore = result.skipped && !!result.restorable;
    youtubeSeriesSkip.hidden = !(result.inherited && (!result.skipped || result.restorable));
    youtubeSeriesSkip.dataset.mode = restore ? "restore" : "skip";
    if (youtubeSeriesSkip.dataset.skipLabel === undefined) {
      youtubeSeriesSkip.dataset.skipLabel = youtubeSeriesSkip.textContent ?? "";
    }
    youtubeSeriesSkip.textContent = restore ? "Restore YouTube on this date" : youtubeSeriesSkip.dataset.skipLabel;
    youtubeSeriesStop.hidden = !result.active;
  } catch {
    if (selection !== active) return;
    youtubeSeriesSkip.hidden = true;
    youtubeSeriesSkip.dataset.mode = "";
    youtubeSeriesStop.hidden = true;
  }
}
async function refreshLinkedinSeries() {
  if (!active) return;
  const selection = active;
  const revision = linkedinAccountRevision;
  try {
    const result = await request<{ active: boolean; inherited: boolean; skipped: boolean; restorable?: boolean }>(mediaPath(`destinations/${linkedinProvider()}/series`));
    if (selection !== active || revision !== linkedinAccountRevision) return;
    const restore = result.skipped && !!result.restorable;
    linkedinSeriesSkip.hidden = !(result.inherited && (!result.skipped || result.restorable));
    linkedinSeriesSkip.dataset.mode = restore ? "restore" : "skip";
    if (linkedinSeriesSkip.dataset.skipLabel === undefined) {
      linkedinSeriesSkip.dataset.skipLabel = linkedinSeriesSkip.textContent ?? "";
    }
    linkedinSeriesSkip.textContent = restore ? "Restore LinkedIn on this date" : linkedinSeriesSkip.dataset.skipLabel;
    linkedinSeriesStop.hidden = !result.active;
  } catch {
    if (selection !== active || revision !== linkedinAccountRevision) return;
    linkedinSeriesSkip.hidden = true;
    linkedinSeriesSkip.dataset.mode = "";
    linkedinSeriesStop.hidden = true;
  }
}
async function changeLinkedinSeries(date: boolean, restore = false) {
  const selection = active;
  if (!selection || selection.event.event.schedule.repeat === "none") return;
  const revision = linkedinAccountRevision;
  await request(mediaPath(`destinations/${linkedinProvider()}/series`), restore ? "PUT" : "DELETE", {date});
  if (selection !== active || revision !== linkedinAccountRevision) return;
  await refreshLinkedinSeries();
  await refreshDestinations();
}
linkedinSeriesSkip.onclick = () => void action(() => changeLinkedinSeries(true, linkedinSeriesSkip.dataset.mode === "restore"));
linkedinSeriesStop.onclick = () => void action(() => changeLinkedinSeries(false));
destinations.ontoggle = () => {
  if (!destinations.open) return;
  void action(async () => {
    await refreshDestinations();
    if (!youtubeSeriesLabel.hidden) await refreshYouTubeSeries();
  });
};
button("refreshDestinations").onclick = () => void action(() => refreshDestinations());
async function loadYouTubeBroadcasts(more = false) {
  if (!active) return;
  const selection = active;
  youtubeStatus.textContent = "Loading YouTube broadcasts…";
  try {
    const path = more && youtubeNext ? `${mediaPath("destinations/youtube")}?cursor=${encodeURIComponent(youtubeNext)}` : mediaPath("destinations/youtube");
    const result = await request<YouTubeBroadcasts>(path);
    if (selection !== active) return;
    if (!more) youtubeList.replaceChildren();
    for (const item of result.items) {
      const option = new Option(`${item.title} · ${new Date(item.starts).toLocaleString()} · ${item.privacy}`, item.id);
      option.dataset.channel = item.channel;
      youtubeList.append(option);
    }
    youtubeNext = result.next;
    youtubeMore.hidden = !youtubeNext;
    youtubeSubmit.disabled = !youtubeList.options.length;
    youtubeStatus.textContent = result.items.length ? "" : "No scheduled YouTube broadcasts found.";
  } catch (error) {
    if (selection === active) youtubeStatus.textContent = error instanceof Error ? error.message : String(error);
  }
}
button("loadYoutubeBroadcasts").onclick = () => void action(() => loadYouTubeBroadcasts());
youtubeMore.onclick = () => void action(() => loadYouTubeBroadcasts(true));
youtubeSeriesSkip.onclick = () => {
  if (!active) return;
  const selection = active;
  const restore = youtubeSeriesSkip.dataset.mode === "restore";
  void action(async () => {
    await request<void>(mediaPath("destinations/youtube/series"), restore ? "PUT" : "DELETE", { date: true });
    if (selection !== active) return;
    await refreshYouTubeSeries();
    await refreshDestinations();
  });
};
youtubeSeriesStop.onclick = () => {
  if (!active) return;
  const selection = active;
  void action(async () => {
    await request<void>(mediaPath("destinations/youtube/series"), "DELETE", { date: false });
    if (selection !== active) return;
    await refreshYouTubeSeries();
    await refreshDestinations();
  });
};
youtubeForm.onsubmit = event => {
  event.preventDefault();
  const selected = youtubeList.selectedOptions[0];
  if (!selected?.dataset.channel) return;
  void action(async () => {
    await request<{id:string}>(mediaPath("destinations/youtube"), "POST", {broadcast: selected.value, channel: selected.dataset.channel, series: !youtubeAddSeriesLabel.hidden && youtubeAddSeries.checked});
    youtubeStatus.textContent = "YouTube destination added.";
    await refreshDestinations();
    await refreshYouTubeSeries();
  });
};
youtubeCreate.onsubmit = event => {
  event.preventDefault();
  if (!active || youtubeCreateButton.disabled) return;
  const path = mediaPath("destinations/youtube/create");
  const formData = new FormData(youtubeCreate);
  const privacy = formData.get("privacy");
  const series = formData.get("series") === "on";
  void action(async () => {
    youtubeCreateButton.disabled = true;
    try {
      const result = await request<{state: "complete" | "pending"}>(path, "POST", {privacy, series});
      if (!active || mediaPath("destinations/youtube/create") !== path) return;
      if (result.state === "complete") {
        youtubeStatus.textContent = "YouTube broadcast created.";
        youtubeCreateButton.textContent = "Create YouTube broadcast";
        await refreshDestinations();
        await refreshYouTubeSeries();
      } else {
        youtubeStatus.textContent = "Waiting for YouTube to confirm the broadcast.";
        youtubeCreateButton.textContent = "Check creation status";
      }
    } finally {
      youtubeCreateButton.disabled = false;
    }
  });
};
async function refreshFacebookAutoStatus() {
  if (!active || !facebookPage.value) return;
  const revision = ++facebookStatusRevision;
  const path = facebookBasePath();
  const page = facebookPage.value;
  const privacyBefore = facebookPrivacy.value;
  facebookStatus.textContent = "";
  facebookCancel.disabled = true;
  facebookRestoreDate.hidden = true;
  facebookSeriesStop.disabled = true;
  try {
    const result = await request<{state: string; series: boolean; privacy?: string | null; restorable?: boolean}>(`${path}/auto?page=${encodeURIComponent(page)}`);
    if (revision !== facebookStatusRevision || path !== facebookBasePath()) return;
    const labels: Record<string, string> = {
      queued: "Facebook Live will start when this occurrence is ready to stream.",
      prepared: "Facebook Live delivery is configured for this occurrence.",
      unconfirmed: "Facebook has not confirmed the broadcast. Check Live Producer.",
      failed: "CuePorch could not start Facebook Live. Check the Facebook connection and try again.",
    };
    facebookStatus.textContent = result.restorable
      ? "Facebook Live start is skipped for this occurrence."
      : labels[result.state] ?? "";
    facebookCancel.disabled = !["queued", "failed"].includes(result.state);
    facebookRestoreDate.hidden = !result.restorable;
    facebookSeriesStop.disabled = !result.series;
    if (facebookProfile() && facebookPrivacy.value === privacyBefore) {
      facebookPrivacy.value = result.privacy ?? "";
    }
  } catch (error) {
    if (revision === facebookStatusRevision && path === facebookBasePath()) throw error;
  }
}

facebookTarget.onchange = () => {
  clearFacebookAsyncStatus();
  facebookPage.replaceChildren();
  facebookSubmit.disabled = true;
  const profile = facebookProfile();
  facebookPageLabel.textContent = profile ? "Facebook profile" : "Facebook Live Page";
  facebookLoad.textContent = profile ? "Load Facebook profiles" : "Load Facebook Live Pages";
  facebookPrivacyLabel.hidden = !profile;
};
facebookPage.onchange = () => void action(async () => {
  clearFacebookAsyncStatus();
  if (facebookPage.value) await refreshFacebookAutoStatus();
});
facebookAutomatic.onchange = () => { facebookSeries.disabled = !facebookAutomatic.checked; };
facebookLoad.onclick = () => void action(async () => {
  if (!active) return;
  const path = facebookBasePath();
  const endpoint = facebookProfile()
    ? "/api/integrations/facebook-profile-live/profiles" : "/api/integrations/facebook-live/pages";
  const revision = ++facebookStatusRevision;
  facebookStatus.textContent = "";
  facebookCancel.disabled = true;
  facebookRestoreDate.hidden = true;
  facebookSeriesStop.disabled = true;
  facebookSubmit.disabled = true;
  try {
    const result = await request<{items: {id: string; name: string}[]}>(endpoint);
    if (!active || revision !== facebookStatusRevision || path !== facebookBasePath()) return;
    facebookPage.replaceChildren(...result.items.map(page => new Option(page.name, page.id)));
    facebookSubmit.disabled = !result.items.length;
    await refreshFacebookAutoStatus();
  } catch (error) {
    if (revision === facebookStatusRevision && path === facebookBasePath()) throw error;
  }
});
facebookForm.onsubmit = event => {
  event.preventDefault();
  if (!active || facebookSubmit.disabled || !facebookPage.value) return;
  const path = facebookBasePath();
  const page = facebookPage.value;
  const automatic = facebookAutomatic.checked;
  const series = automatic && !facebookSeriesControls.hidden && facebookSeries.checked;
  const privacy = facebookProfile() ? (facebookPrivacy.value || null) : null;
  const revision = ++facebookStatusRevision;
  void action(async () => {
    facebookSubmit.disabled = true;
    try {
      const result = await request<{state: "prepared" | "unconfirmed" | "queued"}>(
        automatic ? path + "/auto" : path,
        automatic ? "PUT" : "POST",
        facebookProfile() ? {page, series, privacy} : {page, series});
      if (revision !== facebookStatusRevision || path !== facebookBasePath()) return;
      facebookRestoreDate.hidden = true;
      facebookCancel.disabled = result.state !== "queued";
      if (result.state === "queued") {
        facebookStatus.textContent = "Facebook Live will start when this occurrence is ready to stream.";
        if (series) {
          facebookSeriesStop.disabled = false;
        }
      } else if (result.state === "prepared") {
        facebookStatus.textContent = "Facebook Live prepared. Preview and publish in Facebook Live Producer.";
        await refreshDestinations();
      } else {
        facebookStatus.textContent = "Facebook has not confirmed the broadcast. Check Live Producer.";
      }
    } finally {
      if (active && revision === facebookStatusRevision && path === facebookBasePath()) {
        facebookSubmit.disabled = !facebookPage.options.length;
      }
    }
  });
};
facebookCancel.onclick = () => void action(async () => {
  if (!active || !facebookPage.value || facebookCancel.disabled) return;
  const path = facebookBasePath();
  const page = facebookPage.value;
  const revision = ++facebookStatusRevision;
  facebookCancel.disabled = true;
  try {
    await request(path + "/auto", "DELETE", {page});
    if (revision !== facebookStatusRevision || path !== facebookBasePath()) return;
    await refreshFacebookAutoStatus();
    if (facebookRestoreDate.hidden) facebookStatus.textContent = "Automatic Facebook Live start canceled.";
  } catch (error) {
    if (revision === facebookStatusRevision && path === facebookBasePath()) facebookCancel.disabled = false;
    throw error;
  }
});
facebookRestoreDate.onclick = () => void action(async () => {
  if (!active || !facebookPage.value || facebookRestoreDate.hidden) return;
  const path = facebookBasePath();
  const page = facebookPage.value;
  await request(path + "/auto", "PUT", {page, restore: true});
  if (path === facebookBasePath() && page === facebookPage.value) await refreshFacebookAutoStatus();
});
facebookSeriesStop.onclick = () => void action(async () => {
  if (!active || !facebookPage.value || facebookSeriesStop.disabled) return;
  const revision = ++facebookStatusRevision;
  const path = facebookBasePath();
  const page = facebookPage.value;
  facebookSeriesStop.disabled = true;
  try {
    await request(path + "/auto", "DELETE", {page, series: true});
    if (revision !== facebookStatusRevision || path !== facebookBasePath()) return;
    await refreshFacebookAutoStatus();
  } catch (error) {
    if (revision === facebookStatusRevision && path === facebookBasePath()) facebookSeriesStop.disabled = false;
    throw error;
  }
});
button("loadLinkedinCompany").onclick = () => void action(async () => {
  const selection = active;
  if (!selection) return;
  const revision = linkedinAccountRevision;
  const profile = linkedinAccount.value === "member";
  linkedinCompany.replaceChildren();
  linkedinSubmit.disabled = true;
  const account = await request<{id: string}>(profile
    ? "/api/integrations/linkedin-member-live/profile" : "/api/integrations/linkedin-live/company");
  if (selection !== active || revision !== linkedinAccountRevision) return;
  linkedinCompany.append(new Option((profile ? "Profile " : "Company Page ") + account.id, account.id));
  linkedinSubmit.disabled = false;
  if (!linkedinSeriesLabel.hidden) await refreshLinkedinSeries();
});
linkedinForm.onsubmit = event => {
  event.preventDefault();
  const selection = active;
  if (!selection || linkedinSubmit.disabled || !linkedinCompany.value) return;
  const revision = linkedinAccountRevision;
  const path = mediaPath(`destinations/${linkedinProvider()}/schedule`);
  const account = linkedinAccount.value === "member"
    ? {profile: linkedinCompany.value} : {company: linkedinCompany.value};
  const auto_prepare = linkedinAutomaticDelivery.checked;
  const series = !linkedinSeriesLabel.hidden && linkedinSeries.checked;
  void action(async () => {
    linkedinSubmit.disabled = true;
    linkedinStatus.textContent = "";
    linkedinPost.hidden = true;
    linkedinPost.removeAttribute("href");
    try {
      const result = await request<{state: "scheduled" | "pending" | "unconfirmed"; post_urn?: string;
        auto_prepare?: boolean}>(path, "POST", {...account, auto_prepare, series});
      if (selection !== active || revision !== linkedinAccountRevision) return;
      const urn = result.post_urn ?? "";
      if (result.state === "scheduled" && /^urn:li:ugcPost:[0-9]{1,30}$/.test(urn)) {
        linkedinStatus.textContent = result.auto_prepare
          ? "Announced on LinkedIn. CuePorch will prepare delivery when this occurrence is ready to stream."
          : "Announced on LinkedIn. Use Prepare LinkedIn delivery when ready.";
        linkedinPost.href = "https://www.linkedin.com/feed/update/" + urn + "/";
        linkedinPost.hidden = false;
      } else if (result.state === "pending") {
        linkedinStatus.textContent = "Another request is scheduling this occurrence.";
      } else {
        linkedinStatus.textContent = "LinkedIn did not confirm the announcement. Check the connected account.";
      }
      if (result.state === "scheduled") await refreshLinkedinSeries();
    } finally {
      if (selection === active && revision === linkedinAccountRevision) linkedinSubmit.disabled = !linkedinCompany.value;
    }
  });
};
async function loadLinkedinEvents(append: boolean) {
  const selection = active;
  const revision = linkedinAccountRevision;
  if (!selection || (append && existingLinkedinNext === null)) return;
  const start = append ? existingLinkedinNext! : 0;
  if (!append) {
    existingLinkedinList.replaceChildren();
    existingLinkedinMore.hidden = true;
    existingLinkedinChoose.disabled = true;
    existingLinkedinNext = null;
  }
  const result = await request<{items: {id: string; title: string}[]; next: number | null}>(
    mediaPath(`destinations/${linkedinProvider()}/existing`) + "?start=" + start);
  if (selection !== active || revision !== linkedinAccountRevision) return;
  for (const item of result.items) {
    const option = new Option(item.title, item.id);
    option.dataset.start = String(start);
    existingLinkedinList.append(option);
  }
  existingLinkedinNext = result.next;
  existingLinkedinMore.hidden = result.next === null;
  existingLinkedinChoose.disabled = !existingLinkedinList.options.length;
  linkedinStatus.textContent = !existingLinkedinList.options.length && result.next === null
    ? "No available LinkedIn events start at this time." : "";
}
button("loadExistingLinkedin").onclick = () => void action(() => loadLinkedinEvents(false));
existingLinkedinMore.onclick = () => void action(() => loadLinkedinEvents(true));
existingLinkedinChoose.onclick = () => void action(async () => {
  const selection = active;
  const revision = linkedinAccountRevision;
  const option = existingLinkedinList.selectedOptions[0];
  if (!selection || !option?.dataset.start) return;
  const result = await request<{post_urn: string | null; auto_prepare: boolean}>(
    mediaPath(`destinations/${linkedinProvider()}/existing`), "POST", {
      id: option.value, start: Number(option.dataset.start), auto_prepare: linkedinAutomaticDelivery.checked,
      series: !linkedinSeriesLabel.hidden && linkedinSeries.checked,
    });
  if (selection !== active || revision !== linkedinAccountRevision) return;
  await refreshLinkedinSeries();
  if (selection !== active || revision !== linkedinAccountRevision) return;
  const urn = result.post_urn ?? "";
  linkedinPost.hidden = !/^urn:li:ugcPost:[0-9]{1,30}$/.test(urn);
  if (linkedinPost.hidden) linkedinPost.removeAttribute("href");
  else linkedinPost.href = "https://www.linkedin.com/feed/update/" + urn + "/";
  linkedinStatus.textContent = result.auto_prepare
    ? "Existing LinkedIn event selected. CuePorch will prepare delivery when this occurrence is ready to stream."
    : "Existing LinkedIn event selected. Use Prepare LinkedIn delivery when ready.";
});
button("prepareLinkedinLive").onclick = () => void action(async () => {
  const selection = active;
  const revision = linkedinAccountRevision;
  const control = button("prepareLinkedinLive");
  if (!selection || control.disabled) return;
  const path = mediaPath(`destinations/${linkedinProvider()}/prepare`);
  control.disabled = true;
  try {
    const result = await request<{state: string}>(path, "POST", {});
    if (selection !== active || revision !== linkedinAccountRevision) return;
    switch (result.state) {
      case "prepared":
        linkedinStatus.textContent = "Ready. CuePorch will publish once LinkedIn confirms the live signal.";
        await refreshDestinations();
        break;
      case "pending":
        linkedinStatus.textContent = "Another request is preparing this stream.";
        break;
      case "closing":
      case "ended":
        linkedinStatus.textContent = "This LinkedIn Live stream has ended or is ending.";
        break;
      default:
        linkedinStatus.textContent = "LinkedIn did not confirm this stream. Check LinkedIn Live Studio.";
    }
  } finally {
    if (selection === active && revision === linkedinAccountRevision) control.disabled = false;
  }
});
destinationForm.onsubmit = event => {
  event.preventDefault();
  const values = new FormData(destinationForm);
  void action(async () => {
    const data = {name: values.get("name"), server: values.get("server"),
      key: values.get("key"), enabled: values.has("enabled"), reusable: values.has("reusable"),
      preset: values.get("preset")};
    const result = await request<Destinations>(mediaPath("destinations"), "POST", data);
    destinationForm.reset();
    await refreshDestinations(result);
  });
};
const presetSelect = destinationForm.elements.namedItem("preset") as HTMLSelectElement;
const patreonDestinationHelp = element<HTMLParagraphElement>("patreonDestinationHelp");
const socialDestinationHelp = element<HTMLParagraphElement>("socialDestinationHelp");
const ottDestinationHelp = element<HTMLParagraphElement>("ottDestinationHelp");
const reusableInput = destinationForm.elements.namedItem("reusable") as HTMLInputElement;
const reusableLabel = element<HTMLLabelElement>("destinationReusableLabel");
const reusableHelp = element<HTMLParagraphElement>("destinationReusableHelp");
function syncPresetHelp() {
  const preset = presetSelect.value;
  patreonDestinationHelp.hidden = preset !== "patreon";
  socialDestinationHelp.hidden = !["instagram", "tiktok", "x", "linkedin"].includes(preset);
  ottDestinationHelp.hidden = preset !== "vimeo-ott";
}
function syncReusable() {
  const hide = presetSelect.value === "custom" || !active || active.event.event.schedule.repeat === "none";
  reusableLabel.hidden = reusableHelp.hidden = hide;
  reusableInput.disabled = hide;
  if (hide) reusableInput.checked = false;
}
presetSelect.addEventListener("change", () => { syncPresetHelp(); syncReusable(); });
destinationForm.addEventListener("reset", () => queueMicrotask(() => { syncPresetHelp(); syncReusable(); }));
syncPresetHelp();
syncReusable();
window.setInterval(() => {
  if (destinations.open && !document.hidden && !pending) {
    void refreshDestinations().catch(() => {});
  }
}, 5000);

function renderProgram() {
  const controls = element<HTMLOListElement>("eventProgramSources");
  controls.replaceChildren();
  const editable = program?.editable ?? false;
  const choices = program?.choices || [];
  for (const [position, source] of programSources.entries()) {
    const item = document.createElement("li");
    const title = choices.find((choice) => choice.work === source.work && choice.cut === source.cut)?.title || "Unavailable video";
    item.append(title + " ");
    for (const [label, step] of [["Move up", -1], ["Move down", 1], ["Remove", 0]] as const) {
      const control = document.createElement("button");
      control.type = "button";
      control.textContent = label;
      control.setAttribute("aria-label", `${label} ${title}`);
      control.disabled = !editable || position + step < 0 || position + step >= programSources.length;
      control.onclick = () => {
        programSources.splice(position, 1);
        if (step) programSources.splice(position + step, 0, source);
        renderProgram();
      };
      item.append(control);
    }
    controls.append(item);
  }
  programChoice.replaceChildren(...choices.map((choice, index) => new Option(choice.title, String(index))));
  programChoice.disabled = !editable || !choices.length;
  button("addProgramVideo").disabled = programChoice.disabled || programSources.length >= 20;
  button("saveEventProgram").disabled = !editable || !programSources.length;
  button("clearEventProgram").disabled = !editable || !program?.revision;
  const state = program?.state.replaceAll("_", " ");
  element("eventProgramStatus").textContent = program ? (program.error || `Program ${state}.`) : "";
}

async function loadProgram() {
  program = await request<EventProgram>(mediaPath("program"));
  programSources = [...program.sources];
  renderProgram();
}

async function writeProgram(sources: ProgramSource[]) {
  program = await request<EventProgram>(mediaPath("program"), "PUT", {revision: program?.revision ?? null, sources});
  programSources = [...program.sources];
  renderProgram();
}

programPanel.ontoggle = () => { if (programPanel.open) void action(loadProgram); };
button("refreshEventProgram").onclick = () => void action(loadProgram);
button("addProgramVideo").onclick = () => {
  const choice = program?.choices[Number(programChoice.value)];
  if (!choice || programSources.length >= 20) return;
  programSources.push({work: choice.work, cut: choice.cut});
  renderProgram();
};
button("saveEventProgram").onclick = () => void action(() => writeProgram(programSources), "Program queued for the scheduled occurrence.");
button("clearEventProgram").onclick = () => void action(() => writeProgram([]), "Manual production selected.");

let chatRequest: Promise<void> | undefined;
async function updateChat(method = "GET", body?: object, fields = false) {
  if (!active || (method === "GET" && !fields && chatRequest)) return;
  const path = mediaPath("chat");
  const previous = chatRequest;
  const pending = previous ? previous.then(() => readChat(path, method, body, fields)) : readChat(path, method, body, fields);
  chatRequest = pending;
  try { await pending; } finally { if (chatRequest === pending) chatRequest = undefined; }
}

async function readChat(path: string, method: string, body: object | undefined, fields: boolean) {
  if (mediaPath("chat") !== path || !chatPanel.open) return;
  try {
    const state = await request<ChatState>(path, method, body);
    if (mediaPath("chat") !== path || !chatPanel.open) return;
    if (fields) {
      chatEnabled.checked = state.enabled;
      chatInterval.value = String(state.interval);
      chatStatus.textContent = method === "PUT" ? "Chat settings saved." : "";
    }
    renderChat(chatMessages, state.messages, (entry) => [
      messageAction("Remove message", entry, () => void updateChat("POST", {type: "remove", id: entry.id})),
      messageAction("Remove and block viewer", entry, () => void updateChat("POST", {type: "ban", id: entry.id})),
    ]);
  } catch (error) {
    chatStatus.textContent = error instanceof Error ? error.message : String(error);
  }
}

chatPanel.ontoggle = async () => {
  if (!chatPanel.open) return;
  chatForm.inert = true;
  try { await updateChat("GET", undefined, true); } finally { chatForm.inert = false; }
};
window.setInterval(() => { if (chatPanel.open && !document.hidden) void updateChat(); }, 3000);
chatForm.onsubmit = (event) => {
  event.preventDefault();
  void updateChat("PUT", {enabled: chatEnabled.checked, interval: Number(chatInterval.value)}, true);
};
button("exportEventChat").onclick = () => void action(() => downloadEventFile(mediaPath("chat/export"), "cueporch-event-chat.csv"));

button("readEventHealth").onclick = () => void action(async () => {
  const result = await request<EventHealth>(mediaPath("health"));
  const settings = result.delivery_settings;
  const lines = [result.state.replaceAll("_", " ")];
  if (typeof result.viewers === "number") lines.push(`${result.viewers} viewers`);
  if (["live", "waiting_for_encoder", "preparing_replay", "replay", "ended"].includes(result.state)) {
    button("withdrawEvent").textContent = "Withdraw this occurrence";
  }
  if (settings?.latency) lines.push(`${settings.latency} latency mode`);
  if (typeof settings?.dvr_seconds === "number") lines.push(settings.dvr_seconds ? `DVR up to ${settings.dvr_seconds} s` : "DVR off");
  health.textContent = lines.join(" · ");
  measurements.textContent = relaySummary(result, result.state === "live" ? "Encoder measurements unavailable from this host." : "");
});
let analyticsPending = false;
async function refreshAnalytics() {
  if (!active || analyticsPending) return;
  const selection = active;
  analyticsPending = true;
  try {
    const stats = await request<Record<"live" | "replay", AudienceMetrics>>(mediaPath("analytics"));
    if (selection !== active) return;
    analyticsRows.replaceChildren();
    for (const phase of ["live", "replay"] as const) {
      const metrics = stats[phase];
      const row = analyticsRows.insertRow();
      const title = document.createElement("th");
      title.scope = "row";
      title.textContent = phase === "live" ? "Live" : "Replay";
      row.append(title);
      for (const count of [metrics.current, metrics.peak, metrics.loads, metrics.plays]) {
        row.insertCell().textContent = count.toLocaleString();
      }
      for (const seconds of [metrics.watched_seconds, metrics.average_seconds_per_load]) {
        row.insertCell().textContent = `${seconds.toLocaleString(undefined, {maximumFractionDigits: 1})} s`;
      }
    }
    analyticsStatus.textContent = "";
  } catch (error) {
    if (selection === active) analyticsStatus.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    analyticsPending = false;
  }
}
analytics.ontoggle = () => { if (analytics.open) void refreshAnalytics(); };
button("refreshEventAnalytics").onclick = () => void refreshAnalytics();
window.setInterval(() => { if (analytics.open && !document.hidden) void refreshAnalytics(); }, 5000);

async function updateCaptionSettings(update = false) {
  const value = {enabled: captionEnabled.checked, language: captionLanguage.value};
  const result = await request<ProductionCaptions>(mediaPath("captions"), update ? "PUT" : "GET", update ? value : undefined);
  captionEnabled.checked = result.enabled;
  captionLanguage.value = result.language;
  const detail = {
    waiting_for_stream: "Waiting for the event stream",
    engine_unavailable: "Connect a local TextFlowKit streaming runtime",
    listening: "Transcribing the program",
    blocked: "Automatic caption generation is disabled in workspace AI settings",
    media_or_engine_unavailable: "Waiting for usable live audio and a local transcription runtime",
    stopped: "Captioning stopped",
  } as Record<string, string>;
  captionStatus.textContent = result.enabled ? `${detail[result.status] || result.status} · ${result.cue_count} lines generated`
    : "Automatic captions are off.";
}
captionSettings.ontoggle = () => { if (captionSettings.open) void action(() => updateCaptionSettings()); };
captionForm.onsubmit = event => { event.preventDefault(); void action(() => updateCaptionSettings(true)); };
button("refreshLiveCaptions").onclick = () => void action(() => updateCaptionSettings());

async function updateReplayCaptions(data?: FormData) {
  if (data) data.set("revision", replayCaptionRevision);
  const result = await request<ReplayCaptions>(mediaPath("replay-captions"), data ? "POST" : "GET", data);
  replayCaptionRevision = result.revision;
  (replayCaptionForm.elements.namedItem("language") as HTMLInputElement).value = result.language;
  button("downloadReplayCaptions").disabled = !result.has_track;
  button("saveReplayCaptions").disabled = !result.archived;
  const states: Record<string, string> = {
    queued: "Caption generation is queued.",
    running: "Transcribing the finished recording.",
    blocked: "Workspace AI settings blocked generation. You can upload a subtitle file.",
    failed: "Could not generate captions. You can upload a subtitle file.",
    no_speech: "The caption engine did not find speech.",
    unavailable: result.archived ? "No caption track has been saved." : "The recording is still being prepared.",
  };
  replayCaptionStatus.textContent = result.has_track
    ? (result.published ? "Replay captions are ready." : "Replay captions are awaiting approval.")
    : states[result.state];
}

replayCaptions.ontoggle = () => { if (replayCaptions.open) void action(() => updateReplayCaptions()); };
button("refreshReplayCaptions").onclick = () => void action(() => updateReplayCaptions());
button("downloadReplayCaptions").onclick = () => void action(() =>
  downloadEventFile(mediaPath("replay-captions") + "?download=1", "cueporch-replay-captions.vtt"));
replayCaptionForm.onsubmit = event => {
  event.preventDefault();
  const data = new FormData(replayCaptionForm);
  void action(async () => {
    await updateReplayCaptions(data);
    (replayCaptionForm.elements.namedItem("track") as HTMLInputElement).value = "";
  });
};

async function updateLibraryRecording(save = false) {
  const result = await request<LibraryRecording>(mediaPath("library-recording"), save ? "POST" : "GET");
  const states: Record<string, string> = {
    available: "You can keep a private Library copy when recording finishes.",
    unavailable: "Prepare an occurrence with replay recording enabled to save a Library copy.",
    waiting: "Waiting for the hosted replay.",
    preparing: "Copying the finished recording.",
    failed: result.error || "Could not save the recording.",
    ready: "Saved privately in the Library.",
  };
  libraryRecordingStatus.textContent = states[result.state];
  saveLibraryRecording.disabled = !["available", "failed"].includes(result.state);
  saveLibraryRecording.textContent = result.state === "failed" ? "Retry saving to Library" : "Save to Library";
  libraryRecordingLink.hidden = !result.work;
  if (result.work) libraryRecordingLink.href = "/operator/videos/" + result.work;
}

libraryRecording.ontoggle = () => { if (libraryRecording.open) void action(() => updateLibraryRecording()); };
button("refreshLibraryRecording").onclick = () => void action(() => updateLibraryRecording());
saveLibraryRecording.onclick = () => void action(() => updateLibraryRecording(true));

async function updatePlaylist(save = false) {
  const body = save ? Object.fromEntries(playlistOptions.map(name => [name, playlistOption(name).checked])) : undefined;
  const result = await request<PlaylistOptions>(`${root}/${active!.event.id}/playlist`, save ? "PUT" : "GET", body);
  for (const name of playlistOptions) playlistOption(name).checked = result[name];
  button("saveEventPlaylist").disabled = false;
  eventPlaylistStatus.textContent = save ? "Replay playlist settings saved." : "";
}

eventPlaylistPanel.ontoggle = () => { if (eventPlaylistPanel.open) void action(() => updatePlaylist()); };
eventPlaylistForm.onsubmit = (event) => { event.preventDefault(); void action(() => updatePlaylist(true)); };

button("withdrawEvent").onclick = () => void action(async () => {
  encoder.hidden = true;
  element("dashSettings").hidden = true;
  input("dashUploadToken").value = "";
  key.value = "";
  backupKey.value = "";
  srtBackupSecret.value = "";
  await request(mediaPath("delivery"), "DELETE");
  chooseInterpreterOccurrence(null);
  await load();
}, "Occurrence withdrawn.");
