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
  private sharing = false;
  private running = false;
  private position = 0;

  constructor() {
    this.script.oninput = () => {
      this.view.textContent = this.script.value;
      this.reset();
      this.update();
    };
    this.font.oninput = () => { this.view.style.fontSize = this.font.value + "px"; };
    this.speed.onchange = () => {
      if (this.speed.value === "0") this.running = false;
      this.update();
    };
    this.play.onclick = () => { this.running = true; this.update(); };
    this.pause.onclick = () => this.stop();
    this.restart.onclick = () => this.reset();
    this.update();
  }

  screenSharing(enabled: boolean) {
    this.sharing = enabled;
    if (enabled) this.running = false;
    this.update();
  }

  start() {
    this.reset();
    if (this.view.hidden) return;
    this.panel.open = true;
    this.running = this.speed.value !== "0";
    this.update();
  }

  stop() {
    this.running = false;
    this.update();
  }

  scroll(elapsed: number, recordingPaused: boolean) {
    if (!this.running || this.view.hidden || !this.panel.open || recordingPaused) return;
    if (Math.abs(this.view.scrollTop - this.position) > 1) this.position = this.view.scrollTop;
    const end = Math.max(0, this.view.scrollHeight - this.view.clientHeight);
    this.position = Math.min(end, this.position + Math.min(100, elapsed) * .023 * Number(this.speed.value));
    this.view.scrollTop = this.position;
    if (end > 0 && this.position === end) this.stop();
  }

  private reset() {
    this.position = this.view.scrollTop = 0;
  }

  private update() {
    const empty = !this.script.value.trim();
    this.view.hidden = this.sharing || empty;
    this.screenNotice.hidden = !this.sharing || empty;
    this.play.disabled = this.view.hidden || this.running || this.speed.value === "0";
    this.pause.disabled = this.view.hidden || !this.running;
    this.restart.disabled = this.view.hidden;
  }
}
