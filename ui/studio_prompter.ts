import {StudioVoice} from "./studio_voice.js";

const arrowMoves: Record<string, [number, number]> = {
  ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
};

function words(text: string) {
  return Array.from(text.matchAll(/[\p{L}\p{N}\p{M}]+/gu), match => ({
    text: match[0].normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase(), offset: match.index,
  }));
}

/** Scroll an operator-only script independently of the capture canvas. */
export class StudioPrompter {
  private readonly panel = document.querySelector<HTMLDetailsElement>("#recordPrompterSettings")!;
  private readonly script = document.querySelector<HTMLTextAreaElement>("#recordScript")!;
  private readonly view = document.querySelector<HTMLElement>("#recordPrompter")!;
  private readonly speed = document.querySelector<HTMLSelectElement>("#recordScroll")!;
  private readonly font = document.querySelector<HTMLInputElement>("#recordFont")!;
  private readonly play = document.querySelector<HTMLButtonElement>("#recordPrompterPlay")!;
  private readonly pause = document.querySelector<HTMLButtonElement>("#recordPrompterPause")!;
  private readonly restart = document.querySelector<HTMLButtonElement>("#recordPrompterReset")!;
  private readonly screenNotice = document.querySelector<HTMLElement>("#recordPrompterScreen")!;
  private readonly voiceNotice = document.querySelector<HTMLElement>("#recordPrompterVoice")!;
  private readonly frame = document.querySelector<HTMLElement>("#recordPrompterWindow")!;
  private readonly float = document.querySelector<HTMLInputElement>("#floatPrompter")!;
  private readonly move = document.querySelector<HTMLButtonElement>("#movePrompter")!;
  private sharing = false;
  private recordingPaused = false;
  private running = false;
  private position = 0;
  private voice?: StudioVoice;
  private voiceTrack?: MediaStreamTrack;
  private scriptWords: ReturnType<typeof words> = [];
  private spokenWord = "";
  private cursor = 0;
  private floatingAt = {left: 16, top: Math.round(window.innerHeight / 6)};
  private drag?: {pointer: number; x: number; y: number};

  constructor(private microphone: () => MediaStreamTrack | undefined, private credential: () => string,
              private endpoint = "/api/studio/teleprompter/voice") {
    this.script.oninput = () => {
      if (this.speed.value === "auto") this.stop();
      this.view.textContent = this.script.value;
      this.scriptWords = words(this.script.value);
      this.reset();
      this.update();
    };
    this.font.oninput = () => { this.view.style.fontSize = this.font.value + "px"; };
    this.speed.onchange = () => {
      this.voiceNotice.textContent = "";
      if (this.speed.value === "0") this.running = false;
      this.update();
    };
    this.play.onclick = () => { this.running = true; this.update(); };
    this.pause.onclick = () => this.stop();
    this.restart.onclick = () => { this.reset(); this.update(); };
    this.panel.ontoggle = () => { this.place(this.floatingAt.left, this.floatingAt.top); this.syncVoice(); };
    this.float.onchange = () => {
      this.frame.classList.toggle("floating", this.float.checked);
      this.move.hidden = !this.float.checked;
      if (!this.float.checked) this.view.style.removeProperty("width");
      this.place(this.floatingAt.left, this.floatingAt.top);
    };
    this.move.onpointerdown = event => {
      if (event.button !== 0) return;
      this.move.focus();
      this.move.setPointerCapture(event.pointerId);
      this.drag = {pointer: event.pointerId, x: event.clientX - this.floatingAt.left,
        y: event.clientY - this.floatingAt.top};
    };
    this.move.onpointermove = event => {
      if (this.drag?.pointer === event.pointerId) this.place(event.clientX - this.drag.x, event.clientY - this.drag.y);
    };
    this.move.onlostpointercapture = () => { this.drag = undefined; };
    this.move.onkeydown = event => {
      const offsets = arrowMoves[event.key];
      if (!offsets) return;
      event.preventDefault();
      const step = event.shiftKey ? 40 : 10;
      this.place(this.floatingAt.left + offsets[0] * step, this.floatingAt.top + offsets[1] * step);
    };
    new ResizeObserver(() => this.place(this.floatingAt.left, this.floatingAt.top)).observe(this.frame);
    window.addEventListener("resize", () => this.place(this.floatingAt.left, this.floatingAt.top));
    window.addEventListener("pagehide", () => this.closeVoice());
    this.update();
  }

  show(available: boolean) {
    this.panel.hidden = !available;
    if (!available) this.stop();
  }

  setContext(sharing: boolean, recordingPaused: boolean) {
    this.sharing = sharing;
    this.recordingPaused = recordingPaused;
    if (sharing) this.running = false;
    this.update();
  }

  start() {
    this.reset();
    if (this.frame.hidden) return;
    this.panel.open = true;
    this.running = this.speed.value !== "0";
    this.update();
  }

  stop() {
    this.running = false;
    this.update();
  }

  scroll(elapsed: number) {
    if (!this.running || this.frame.hidden || !this.panel.open || this.recordingPaused || this.speed.value === "auto") return;
    if (Math.abs(this.view.scrollTop - this.position) > 1) this.position = this.view.scrollTop;
    const end = Math.max(0, this.view.scrollHeight - this.view.clientHeight);
    this.position = Math.min(end, this.position + Math.min(100, elapsed) * .023 * Number(this.speed.value));
    this.view.scrollTop = this.position;
    if (end > 0 && this.position === end) this.stop();
  }

  private reset() {
    this.closeVoice();
    this.cursor = 0;
    this.position = this.view.scrollTop = 0;
  }

  private place(left: number, top: number) {
    if (!this.float.checked || this.frame.hidden || !this.panel.open || this.panel.hidden) return;
    const margin = 6;
    this.floatingAt = {
      left: Math.max(margin, Math.min(left, window.innerWidth - this.frame.offsetWidth - margin)),
      top: Math.max(margin, Math.min(top, window.innerHeight - this.frame.offsetHeight - margin)),
    };
    this.frame.style.left = this.floatingAt.left + "px";
    this.frame.style.top = this.floatingAt.top + "px";
  }

  private closeVoice() {
    this.voice?.close();
    this.voice = this.voiceTrack = undefined;
    this.spokenWord = "";
  }

  private syncVoice() {
    if (this.speed.value !== "auto") {
      this.closeVoice();
      this.voiceNotice.textContent = "";
      return;
    }
    if (!this.running || this.frame.hidden || this.panel.hidden || !this.panel.open || this.recordingPaused) {
      if (this.voice) this.voiceNotice.textContent = "Voice follow paused.";
      this.closeVoice();
      return;
    }
    const track = this.microphone();
    if (!track) {
      this.closeVoice();
      this.voiceNotice.textContent = "Connect your microphone to follow your voice.";
      return;
    }
    if (this.voice && this.voiceTrack === track) return;
    this.closeVoice();
    this.voiceTrack = track;
    this.voiceNotice.textContent = "Starting local voice recognition…";
    this.voice = new StudioVoice(this.endpoint, this.credential(), result => {
      if (result.state === "listening") this.voiceNotice.textContent = "Following your voice with local TextFlowKit.";
      for (const line of result.lines) this.follow(line.text);
    }, message => {
      this.stop();
      this.voiceNotice.textContent = message;
    });
    void this.voice.listen(track);
  }

  private follow(text: string) {
    const heard = words(text).map(word => word.text);
    if (!heard.length) return;
    heard.unshift(this.spokenWord);
    this.spokenWord = heard[heard.length - 1];
    const before = this.cursor;
    for (let read = 1; read < heard.length; read++) {
      // Match neighboring words near the current position so a repeated phrase cannot skip a scene.
      const begin = Math.max(0, this.cursor - 1);
      const end = Math.min(this.scriptWords.length - 1, this.cursor + 10);
      for (let check = begin; check < end; check++) {
        if (this.scriptWords[check].text === heard[read - 1]
            && this.scriptWords[check + 1].text === heard[read]) {
          this.cursor = check + 1;
          break;
        }
      }
    }
    if (this.cursor <= before) return;
    const target = this.scriptWords[this.cursor];
    const sentence = this.view.firstChild;
    if (!sentence) return;
    const range = document.createRange();
    range.setStart(sentence, target.offset);
    range.collapse(true);
    this.view.scrollTop += range.getBoundingClientRect().top
      - this.view.getBoundingClientRect().top - this.view.clientHeight / 3;
  }

  private update() {
    const empty = !this.script.value.trim();
    this.frame.hidden = this.sharing || empty;
    this.screenNotice.hidden = !this.sharing || empty;
    this.float.disabled = this.frame.hidden;
    this.play.disabled = this.frame.hidden || this.running || this.speed.value === "0";
    this.pause.disabled = this.frame.hidden || !this.running;
    this.restart.disabled = this.frame.hidden;
    this.syncVoice();
  }
}
