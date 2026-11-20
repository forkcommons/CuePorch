/** Scroll an operator-only script independently of the capture canvas. */
export class StudioPrompter {
    panel = document.querySelector("#recordPrompterSettings");
    script = document.querySelector("#recordScript");
    view = document.querySelector("#recordPrompter");
    speed = document.querySelector("#recordScroll");
    font = document.querySelector("#recordFont");
    play = document.querySelector("#recordPrompterPlay");
    pause = document.querySelector("#recordPrompterPause");
    restart = document.querySelector("#recordPrompterReset");
    screenNotice = document.querySelector("#recordPrompterScreen");
    sharing = false;
    running = false;
    position = 0;
    constructor() {
        this.script.oninput = () => {
            this.view.textContent = this.script.value;
            this.reset();
            this.update();
        };
        this.font.oninput = () => { this.view.style.fontSize = this.font.value + "px"; };
        this.speed.onchange = () => {
            if (this.speed.value === "0")
                this.running = false;
            this.update();
        };
        this.play.onclick = () => { this.running = true; this.update(); };
        this.pause.onclick = () => this.stop();
        this.restart.onclick = () => this.reset();
        this.update();
    }
    screenSharing(enabled) {
        this.sharing = enabled;
        if (enabled)
            this.running = false;
        this.update();
    }
    start() {
        this.reset();
        if (this.view.hidden)
            return;
        this.panel.open = true;
        this.running = this.speed.value !== "0";
        this.update();
    }
    stop() {
        this.running = false;
        this.update();
    }
    scroll(elapsed, recordingPaused) {
        if (!this.running || this.view.hidden || !this.panel.open || recordingPaused)
            return;
        if (Math.abs(this.view.scrollTop - this.position) > 1)
            this.position = this.view.scrollTop;
        const end = Math.max(0, this.view.scrollHeight - this.view.clientHeight);
        this.position = Math.min(end, this.position + Math.min(100, elapsed) * .023 * Number(this.speed.value));
        this.view.scrollTop = this.position;
        if (end > 0 && this.position === end)
            this.stop();
    }
    reset() {
        this.position = this.view.scrollTop = 0;
    }
    update() {
        const empty = !this.script.value.trim();
        this.view.hidden = this.sharing || empty;
        this.screenNotice.hidden = !this.sharing || empty;
        this.play.disabled = this.view.hidden || this.running || this.speed.value === "0";
        this.pause.disabled = this.view.hidden || !this.running;
        this.restart.disabled = this.view.hidden;
    }
}
