/** CuePorch iframe playback API for websites that host the surrounding page. */
export const PLAYER_CHANNEL = "cueporch-player-v1";
export class Player {
    frame;
    origin;
    instance = crypto.getRandomValues(new Uint32Array(2)).join("-");
    sequence = 0;
    pending = new Map();
    events = new Map();
    closed = false;
    constructor(frame) {
        this.frame = frame;
        this.origin = new URL(frame.src, document.baseURI).origin;
        window.addEventListener("message", this.receive);
    }
    receive = ({ source, origin, data }) => {
        if (source !== this.frame.contentWindow || origin !== this.origin || data?.channel !== PLAYER_CHANNEL)
            return;
        if (data.event) {
            for (const listener of this.events.get(data.event) || [])
                listener(data.value);
            return;
        }
        const operation = this.pending.get(data.id);
        if (data.error && operation)
            operation.reject(new Error(data.error));
        else
            operation?.resolve(data.value);
    };
    invoke(method, value, handshake = false) {
        if (this.closed)
            return Promise.reject(new Error("Player has been disconnected."));
        const id = this.instance + "-" + (++this.sequence);
        const post = () => this.frame.contentWindow?.postMessage({ channel: PLAYER_CHANNEL, id, method, value }, this.origin);
        return new Promise((resolve, reject) => {
            const complete = () => {
                clearInterval(retry);
                clearTimeout(timeout);
                this.pending.delete(id);
            };
            const retry = handshake ? window.setInterval(post, 250) : undefined;
            const timeout = window.setTimeout(() => {
                complete();
                reject(new Error("The iframe player did not respond."));
            }, handshake ? 20000 : 10000);
            this.pending.set(id, {
                resolve: (result) => { complete(); resolve(result); },
                reject: (error) => { complete(); reject(error); },
            });
            post();
        });
    }
    ready() { return this.invoke("ready", undefined, true); }
    play() { return this.invoke("play"); }
    pause() { return this.invoke("pause"); }
    getCurrentTime() { return this.invoke("getCurrentTime"); }
    setCurrentTime(seconds) { return this.invoke("setCurrentTime", seconds); }
    getDuration() { return this.invoke("getDuration"); }
    getVolume() { return this.invoke("getVolume"); }
    setVolume(volume) { return this.invoke("setVolume", volume); }
    getMuted() { return this.invoke("getMuted"); }
    setMuted(muted) { return this.invoke("setMuted", muted); }
    getLoop() { return this.invoke("getLoop"); }
    setLoop(loop) { return this.invoke("setLoop", loop); }
    on(event, listener) {
        if (!this.events.has(event))
            this.events.set(event, new Set());
        this.events.get(event).add(listener);
    }
    off(event, listener) {
        if (listener)
            this.events.get(event)?.delete(listener);
        else
            this.events.delete(event);
    }
    disconnect() {
        window.removeEventListener("message", this.receive);
        this.closed = true;
        for (const operation of this.pending.values())
            operation.reject(new Error("Player has been disconnected."));
        this.events.clear();
    }
}
