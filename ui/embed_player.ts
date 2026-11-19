/** CuePorch iframe playback API for websites that host the surrounding page. */

export const PLAYER_CHANNEL = "cueporch-player-v1";
export type PlayerEvent = "play" | "pause" | "ended" | "timeupdate" | "seeking" | "seeked" | "volumechange";
export interface PlaybackEvent {
  seconds: number;
  duration: number | null;
  percent: number;
  volume: number;
  muted: boolean;
}

interface Pending {
  resolve(value: unknown): void;
  reject(error: Error): void;
}

export class Player {
  private readonly origin: string;
  private readonly instance = crypto.getRandomValues(new Uint32Array(2)).join("-");
  private sequence = 0;
  private readonly pending = new Map<string, Pending>();
  private readonly events = new Map<PlayerEvent, Set<(event: PlaybackEvent) => void>>();
  private closed = false;

  constructor(private readonly frame: HTMLIFrameElement) {
    this.origin = new URL(frame.src, document.baseURI).origin;
    window.addEventListener("message", this.receive);
  }

  private receive = ({ source, origin, data }: MessageEvent) => {
    if (source !== this.frame.contentWindow || origin !== this.origin || data?.channel !== PLAYER_CHANNEL) return;
    if (data.event) {
      for (const listener of this.events.get(data.event) || []) listener(data.value);
      return;
    }
    const operation = this.pending.get(data.id);
    if (data.error && operation) operation.reject(new Error(data.error));
    else operation?.resolve(data.value);
  };

  private invoke<T>(method: string, value?: number | boolean, handshake = false): Promise<T> {
    if (this.closed) return Promise.reject(new Error("Player has been disconnected."));
    const id = this.instance + "-" + (++this.sequence);
    const post = () => this.frame.contentWindow?.postMessage({channel: PLAYER_CHANNEL, id, method, value}, this.origin);
    return new Promise<T>((resolve, reject) => {
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
        resolve: (result) => { complete(); resolve(result as T); },
        reject: (error) => { complete(); reject(error); },
      });
      post();
    });
  }

  ready() { return this.invoke<void>("ready", undefined, true); }
  play() { return this.invoke<void>("play"); }
  pause() { return this.invoke<void>("pause"); }
  getCurrentTime() { return this.invoke<number>("getCurrentTime"); }
  setCurrentTime(seconds: number) { return this.invoke<number>("setCurrentTime", seconds); }
  getDuration() { return this.invoke<number>("getDuration"); }
  getVolume() { return this.invoke<number>("getVolume"); }
  setVolume(volume: number) { return this.invoke<number>("setVolume", volume); }
  getMuted() { return this.invoke<boolean>("getMuted"); }
  setMuted(muted: boolean) { return this.invoke<boolean>("setMuted", muted); }
  getLoop() { return this.invoke<boolean>("getLoop"); }
  setLoop(loop: boolean) { return this.invoke<boolean>("setLoop", loop); }

  on(event: PlayerEvent, listener: (event: PlaybackEvent) => void) {
    if (!this.events.has(event)) this.events.set(event, new Set());
    this.events.get(event)!.add(listener);
  }

  off(event: PlayerEvent, listener?: (event: PlaybackEvent) => void) {
    if (listener) this.events.get(event)?.delete(listener);
    else this.events.delete(event);
  }

  disconnect() {
    window.removeEventListener("message", this.receive);
    this.closed = true;
    for (const operation of this.pending.values()) operation.reject(new Error("Player has been disconnected."));
    this.events.clear();
  }
}
