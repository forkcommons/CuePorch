const ENDPOINT = "/api/studio/teleprompter/voice";
const LOCAL_UNAVAILABLE = "Local voice recognition is unavailable. Try another speed or check the TextFlowKit setup.";
export class StudioVoice {
    credential;
    receive;
    unavailable;
    context;
    node;
    session;
    ended = false;
    submitting = false;
    after = 0;
    queued = [];
    trackEvents = new AbortController();
    constructor(credential, receive, unavailable) {
        this.credential = credential;
        this.receive = receive;
        this.unavailable = unavailable;
    }
    headers() {
        return this.credential ? { Authorization: "Bearer " + this.credential } : {};
    }
    async listen(track) {
        try {
            const context = this.context = new AudioContext({ sampleRate: 16000 });
            await context.audioWorklet.addModule("/static/ui/voice_pcm.js");
            await context.resume();
            if (this.ended)
                return;
            const response = await fetch(ENDPOINT, { method: "POST", headers: this.headers(), mode: "same-origin" });
            if (!response.ok)
                throw new Error(LOCAL_UNAVAILABLE);
            this.session = (await response.json()).session;
            if (this.ended) {
                this.closeSession();
                return;
            }
            track.addEventListener("ended", () => this.fail("Microphone disconnected. Play the script to try again."), { once: true, signal: this.trackEvents.signal });
            const source = context.createMediaStreamSource(new MediaStream([track]));
            const node = this.node = new AudioWorkletNode(context, "cueporch-voice-pcm", { channelCount: 1, channelCountMode: "explicit" });
            node.port.onmessage = ({ data }) => {
                if (this.ended)
                    return;
                if (this.queued.length >= 6)
                    this.fail("Voice recognition fell behind. Play the script to try again.");
                else {
                    this.queued.push(data);
                    void this.send();
                }
            };
            node.onprocessorerror = () => this.fail(LOCAL_UNAVAILABLE);
            source.connect(node).connect(context.destination);
        }
        catch {
            this.fail(LOCAL_UNAVAILABLE);
        }
    }
    async send() {
        if (this.submitting)
            return;
        this.submitting = true;
        try {
            while (!this.ended && this.queued.length) {
                const response = await fetch(`${ENDPOINT}/${this.session}?after=${this.after}`, {
                    method: "POST", body: this.queued.shift(), mode: "same-origin",
                    headers: { ...this.headers(), "Content-Type": "application/octet-stream" },
                });
                if (!response.ok)
                    throw new Error(LOCAL_UNAVAILABLE);
                const result = await response.json();
                if (result.state === "unavailable")
                    throw new Error(LOCAL_UNAVAILABLE);
                if (this.ended)
                    return;
                if (result.lines.length)
                    this.after = result.lines[result.lines.length - 1].sequence;
                this.receive(result);
            }
        }
        catch {
            this.fail(LOCAL_UNAVAILABLE);
        }
        finally {
            this.submitting = false;
        }
    }
    fail(message) {
        if (this.ended)
            return;
        this.close();
        this.unavailable(message);
    }
    closeSession() {
        if (!this.session)
            return;
        void fetch(ENDPOINT + "/" + this.session, { method: "DELETE", headers: this.headers(),
            mode: "same-origin", keepalive: true }).catch(() => { });
        this.session = undefined;
    }
    close() {
        if (this.ended)
            return;
        this.ended = true;
        this.trackEvents.abort();
        this.queued.length = 0;
        this.node?.port.close();
        this.node?.disconnect();
        void this.context?.close().catch(() => { });
        this.closeSession();
    }
}
