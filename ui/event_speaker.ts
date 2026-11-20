import {gather, speakerRequest, SpeakerRequestError} from "./speaker_connection.js";
import {speakerChat} from "./speaker_chat.js";
import {readStudioImage, type SlideProgram} from "./live_slides.js";
import {StudioPrompter} from "./studio_prompter.js";

interface GuestState {
  name: string; admitted: boolean; on_air: boolean; revision: string | null; answer: string | null; ice: RTCIceServer[];
  slides: SlideProgram | null;
}
const endpoint = document.body.dataset.speakerEndpoint!;
const button = (id: string) => document.getElementById(id) as HTMLButtonElement;
const join = button("joinSpeaker"), camera = button("toggleSpeakerCamera"), mic = button("toggleSpeakerMic"), leave = button("leaveSpeaker");
const preview = document.querySelector<HTMLVideoElement>("#guestSpeakerPreview")!;
const status = document.querySelector<HTMLElement>("#guestSpeakerStatus")!;
const name = document.querySelector<HTMLElement>("#guestSpeakerName")!;
const presentation = document.querySelector<HTMLDetailsElement>("#guestPresentation")!;
const slideProgram = document.querySelector<HTMLElement>("#guestSlideProgram")!;
const slidePreview = document.querySelector<HTMLImageElement>("#guestSlidePreview")!;
const slideNumber = document.querySelector<HTMLElement>("#guestSlideNumber")!;
const slideUpload = document.querySelector<HTMLFormElement>("#guestSlideUpload")!;
const uploadStatus = document.querySelector<HTMLElement>("#guestSlideUploadStatus")!;
const previousSlide = button("previousGuestSlide"), nextSlide = button("nextGuestSlide");
const slidesPath = `${endpoint}/slides`;
const invite = location.hash.substring(1);
const storageKey = `cueporch-speaker:${endpoint}`;
let token = sessionStorage.getItem(storageKey) || "";
let stream: MediaStream | null = null;
let connection: RTCPeerConnection | null = null;
let revision: string | null = null;
let setup = false;
let timer = 0;
let disconnectedAt = 0;
let shownSlide: SlideProgram | null = null;
let currentImage = "";
let imageRequest: AbortController | undefined;
let movingSlide = false;
const chat = speakerChat(endpoint, () => token);
const prompter = new StudioPrompter(() => stream?.getAudioTracks().find(track => track.enabled && track.readyState === "live"),
  () => token, endpoint + "/teleprompter/voice");
let previousFrame = performance.now();

function scrollPrompt(now: number) {
  prompter.scroll(now - previousFrame);
  previousFrame = now;
  requestAnimationFrame(scrollPrompt);
}
requestAnimationFrame(scrollPrompt);

function stop() {
  clearTimeout(timer);
  connection?.close();
  connection = null;
  revision = null;
  stream?.getTracks().forEach(track => track.stop());
  stream = null;
  preview.srcObject = null;
  preview.hidden = camera.hidden = mic.hidden = leave.hidden = true;
  chat.show(false);
  presentation.hidden = true;
  prompter.show(false);
  showSlides(null);
}

function message(error: unknown) { status.textContent = error instanceof Error ? error.message : String(error); }

function releaseSlideImage() {
  imageRequest?.abort();
  if (currentImage) URL.revokeObjectURL(currentImage);
  currentImage = "";
  slidePreview.removeAttribute("src");
  slidePreview.hidden = true;
}

function showSlides(next: SlideProgram | null) {
  if (!next || next.id !== shownSlide?.id) releaseSlideImage();
  else if (next.revision < shownSlide.revision) return;
  else if (next.revision === shownSlide.revision && (currentImage || imageRequest)) return;
  shownSlide = next;
  slideProgram.hidden = !next;
  if (!next) return;
  previousSlide.disabled = next.page === 0;
  nextSlide.disabled = next.page + 1 >= next.pages;
  slideNumber.textContent = `Slide ${next.page + 1} of ${next.pages}`;
  imageRequest?.abort();
  const pending = new AbortController();
  imageRequest = pending;
  void readStudioImage(`${slidesPath}/${next.id}/${next.revision}`, token,
    AbortSignal.any([pending.signal, AbortSignal.timeout(6000)])).then(({url}) => {
    if (imageRequest !== pending || shownSlide !== next) {
      URL.revokeObjectURL(url);
      return;
    }
    if (currentImage) URL.revokeObjectURL(currentImage);
    currentImage = url;
    slidePreview.src = url;
    slidePreview.hidden = false;
  }).catch(() => {
    if (imageRequest === pending) releaseSlideImage();
  }).finally(() => { if (imageRequest === pending) imageRequest = undefined; });
}

async function turnSlide(direction: number) {
  if (!shownSlide || movingSlide) return;
  const {id, revision} = shownSlide;
  movingSlide = true;
  try {
    const next = await speakerRequest<SlideProgram>(slidesPath, token, "PUT", {id, revision, direction});
    if (shownSlide?.id === id) showSlides(next);
  } catch (error) {
    if (error instanceof SpeakerRequestError && error.status === 404) {
      if (shownSlide?.id === id) showSlides(null);
    } else {
      slideNumber.textContent = String(error);
    }
  } finally { movingSlide = false; }
}

previousSlide.onclick = () => void turnSlide(-1);
nextSlide.onclick = () => void turnSlide(1);
slideUpload.onsubmit = async event => {
  event.preventDefault();
  const submit = slideUpload.querySelector("button")!;
  if (submit.disabled) return;
  submit.disabled = true;
  uploadStatus.textContent = "Preparing the presentation…";
  try {
    const response = await fetch(slidesPath, {method: "POST", headers: {Authorization: `Bearer ${token}`},
      body: new FormData(slideUpload), signal: AbortSignal.timeout(145000)});
    if (!response.ok) {
      const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
      throw new Error(detail || "The presentation could not be shared.");
    }
    const {title}: {title: string} = await response.json();
    slideUpload.reset();
    uploadStatus.textContent = `Shared ${title} with the producer.`;
  } catch (error) {
    uploadStatus.textContent = error instanceof Error ? error.message : String(error);
  } finally { submit.disabled = false; }
};

async function makeOffer(state: GuestState, media: MediaStream) {
  connection?.close();
  disconnectedAt = 0;
  const peer = new RTCPeerConnection({iceServers: state.ice});
  connection = peer;
  for (const track of media.getTracks()) peer.addTrack(track, media);
  try {
    const offer = await gather(peer, await peer.createOffer());
    if (stream !== media || connection !== peer) return;
    revision = (await speakerRequest<{revision: string}>(endpoint, token, "PUT", {offer})).revision;
  } catch (error) {
    peer.close();
    if (connection === peer) connection = null;
    throw error;
  }
}

async function update() {
  const media = stream;
  if (!media) return;
  try {
    const state = await speakerRequest<GuestState>(endpoint, token);
    if (stream !== media) return;
    name.textContent = state.name;
    name.hidden = false;
    chat.show(state.admitted);
    presentation.hidden = !state.admitted;
    prompter.show(state.admitted);
    showSlides(state.slides);
    if (!state.admitted) {
      status.textContent = "Waiting for the producer to admit you.";
      return;
    }
    const health = connection?.connectionState;
    if (health === "disconnected") disconnectedAt ||= performance.now();
    else disconnectedAt = 0;
    if (!connection || health === "failed" || (disconnectedAt && performance.now() - disconnectedAt > 7000)) {
      await makeOffer(state, media);
      status.textContent = "Connecting to the producer.";
      return;
    }
    if (state.revision === revision && state.answer && !connection.remoteDescription) {
      await connection.setRemoteDescription({type: "answer", sdp: state.answer});
    }
    status.textContent = connection.connectionState === "connected"
      ? (state.on_air ? "You are on air." : "Connected to the speaker room. Your feed is off air.")
      : "Connecting to the producer.";
  } catch (error) {
    if (error instanceof SpeakerRequestError && error.status === 404) {
      stop();
      sessionStorage.removeItem(storageKey);
      status.textContent = "Your speaker invitation expired or was removed.";
    } else {
      message(error);
    }
  } finally {
    if (stream === media) timer = window.setTimeout(() => void update(), 1500);
  }
}

join.onclick = async () => {
  if (setup || stream) return;
  setup = join.disabled = true;
  try {
    if (!navigator.mediaDevices) throw new Error("Open the secure speaker link to use a camera and microphone.");
    stream = await navigator.mediaDevices.getUserMedia({video: true, audio: true});
    if (!token) {
      token = (await speakerRequest<{session: string}>(endpoint, invite, "POST")).session;
      sessionStorage.setItem(storageKey, token);
      history.replaceState(null, "", location.pathname);
    }
    preview.srcObject = stream;
    preview.hidden = camera.hidden = mic.hidden = leave.hidden = false;
    join.hidden = true;
    void update();
  } catch (error) {
    stop();
    message(error);
  } finally {
    setup = join.disabled = false;
  }
};

function toggle(kind: "audio" | "video", control: HTMLButtonElement, on: string, off: string) {
  const track = stream?.getTracks().find(item => item.kind === kind);
  if (!track) return;
  track.enabled = !track.enabled;
  control.textContent = track.enabled ? on : off;
}
camera.onclick = () => toggle("video", camera, "Turn camera off", "Turn camera on");
mic.onclick = () => {
  toggle("audio", mic, "Mute microphone", "Unmute microphone");
  prompter.setContext(false, false);
};
leave.onclick = async () => {
  stop();
  sessionStorage.removeItem(storageKey);
  try {
    await speakerRequest(endpoint, token, "DELETE");
    status.textContent = "You left the speaker room.";
  } catch (error) { message(error); }
};
window.addEventListener("pagehide", () => {stop(); chat.close();});
if (!token && !invite) {
  join.disabled = true;
  status.textContent = "Open the complete invitation link to join.";
}
