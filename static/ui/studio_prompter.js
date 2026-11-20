import { StudioVoice } from "./studio_voice.js";
function words(text) {
    return Array.from(text.matchAll(/[\p{L}\p{N}\p{M}]+/gu), match => ({
        text: match[0].normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase(), offset: match.index,
    }));
}
/** Scroll an operator-only script independently of the capture canvas. */
export class StudioPrompter {
    microphone;
    credential;
    endpoint;
    panel = document.querySelector("#recordPrompterSettings");
    script = document.querySelector("#recordScript");
    view = document.querySelector("#recordPrompter");
    speed = document.querySelector("#recordScroll");
    font = document.querySelector("#recordFont");
    play = document.querySelector("#recordPrompterPlay");
    pause = document.querySelector("#recordPrompterPause");
    restart = document.querySelector("#recordPrompterReset");
    screenNotice = document.querySelector("#recordPrompterScreen");
    voiceNotice = document.querySelector("#recordPrompterVoice");
    sharing = false;
    recordingPaused = false;
    running = false;
    position = 0;
    voice;
    voiceTrack;
    scriptWords = [];
    spokenWord = "";
    cursor = 0;
    constructor(microphone, credential, endpoint = "/api/studio/teleprompter/voice") {
        this.microphone = microphone;
        this.credential = credential;
        this.endpoint = endpoint;
        this.script.oninput = () => {
            if (this.speed.value === "auto")
                this.stop();
            this.view.textContent = this.script.value;
            this.scriptWords = words(this.script.value);
            this.reset();
            this.update();
        };
        this.font.oninput = () => { this.view.style.fontSize = this.font.value + "px"; };
        this.speed.onchange = () => {
            this.voiceNotice.textContent = "";
            if (this.speed.value === "0")
                this.running = false;
            this.update();
        };
        this.play.onclick = () => { this.running = true; this.update(); };
        this.pause.onclick = () => this.stop();
        this.restart.onclick = () => { this.reset(); this.update(); };
        this.panel.ontoggle = () => this.syncVoice();
        window.addEventListener("pagehide", () => this.closeVoice());
        this.update();
    }
    show(available) {
        this.panel.hidden = !available;
        if (!available)
            this.stop();
    }
    setContext(sharing, recordingPaused) {
        this.sharing = sharing;
        this.recordingPaused = recordingPaused;
        if (sharing)
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
    scroll(elapsed) {
        if (!this.running || this.view.hidden || !this.panel.open || this.recordingPaused || this.speed.value === "auto")
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
        this.closeVoice();
        this.cursor = 0;
        this.position = this.view.scrollTop = 0;
    }
    closeVoice() {
        this.voice?.close();
        this.voice = this.voiceTrack = undefined;
        this.spokenWord = "";
    }
    syncVoice() {
        if (this.speed.value !== "auto") {
            this.closeVoice();
            this.voiceNotice.textContent = "";
            return;
        }
        if (!this.running || this.view.hidden || this.panel.hidden || !this.panel.open || this.recordingPaused) {
            if (this.voice)
                this.voiceNotice.textContent = "Voice follow paused.";
            this.closeVoice();
            return;
        }
        const track = this.microphone();
        if (!track) {
            this.closeVoice();
            this.voiceNotice.textContent = "Connect your microphone to follow your voice.";
            return;
        }
        if (this.voice && this.voiceTrack === track)
            return;
        this.closeVoice();
        this.voiceTrack = track;
        this.voiceNotice.textContent = "Starting local voice recognition…";
        this.voice = new StudioVoice(this.endpoint, this.credential(), result => {
            if (result.state === "listening")
                this.voiceNotice.textContent = "Following your voice with local TextFlowKit.";
            for (const line of result.lines)
                this.follow(line.text);
        }, message => {
            this.stop();
            this.voiceNotice.textContent = message;
        });
        void this.voice.listen(track);
    }
    follow(text) {
        const heard = words(text).map(word => word.text);
        if (!heard.length)
            return;
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
        if (this.cursor <= before)
            return;
        const target = this.scriptWords[this.cursor];
        const sentence = this.view.firstChild;
        if (!sentence)
            return;
        const range = document.createRange();
        range.setStart(sentence, target.offset);
        range.collapse(true);
        this.view.scrollTop += range.getBoundingClientRect().top
            - this.view.getBoundingClientRect().top - this.view.clientHeight / 3;
    }
    update() {
        const empty = !this.script.value.trim();
        this.view.hidden = this.sharing || empty;
        this.screenNotice.hidden = !this.sharing || empty;
        this.play.disabled = this.view.hidden || this.running || this.speed.value === "0";
        this.pause.disabled = this.view.hidden || !this.running;
        this.restart.disabled = this.view.hidden;
        this.syncVoice();
    }
}
