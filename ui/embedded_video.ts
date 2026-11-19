import { PLAYER_CHANNEL, type PlayerEvent } from "./embed_player.js";

const video = document.querySelector<HTMLVideoElement>("video")!;
let parentOrigin: string | null = null;

function bounded(value: unknown, max: number) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > max) {
    throw new RangeError("That value is outside the player range.");
  }
  return value;
}

function boolean(value: unknown) {
  if (typeof value !== "boolean") throw new TypeError("Choose true or false.");
  return value;
}

function metadata() {
  if (video.readyState >= 1) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const finish = () => {
      video.removeEventListener("loadedmetadata", ready);
      video.removeEventListener("error", failed);
      clearTimeout(timer);
    };
    const ready = () => { finish(); resolve(); };
    const failed = () => { finish(); reject(new Error("Video metadata is not available.")); };
    const timer = setTimeout(failed, 8000);
    video.addEventListener("loadedmetadata", ready);
    video.addEventListener("error", failed);
  });
}

const commands: Record<string, (value: unknown) => unknown> = {
  ready: () => null,
  play: () => video.play(),
  pause: () => video.pause(),
  getCurrentTime: () => video.currentTime,
  setCurrentTime: async (value) => {
    if (video.closest<HTMLElement>(".archive-player")?.dataset.seek === "no") {
      throw new Error("Seeking is disabled for this player.");
    }
    await metadata();
    video.currentTime = bounded(value, video.duration);
    return video.currentTime;
  },
  getDuration: async () => { await metadata(); return video.duration; },
  getVolume: () => video.volume,
  setVolume: (value) => video.volume = bounded(value, 1),
  getMuted: () => video.muted,
  setMuted: (value) => video.muted = boolean(value),
  getLoop: () => video.loop,
  setLoop: (value) => video.loop = boolean(value),
};

if (window.parent !== window) {
  window.addEventListener("message", async ({ source, origin, data }) => {
    if (source !== window.parent || !/^https?:\/\//.test(origin) || data?.channel !== PLAYER_CHANNEL
        || typeof data.id !== "string" || typeof data.method !== "string") return;
    if (data.method === "ready") parentOrigin = origin;
    if (parentOrigin !== origin) return;
    const send = (result: object) => window.parent.postMessage({channel: PLAYER_CHANNEL, id: data.id, ...result}, origin);
    try {
      if (!Object.hasOwn(commands, data.method)) throw new Error("Unknown player operation.");
      const value = await commands[data.method](data.value);
      send({value});
    } catch (error) {
      send({error: error instanceof Error ? error.message : "Playback operation failed."});
    }
  });
  const events: PlayerEvent[] = ["play", "pause", "ended", "timeupdate", "seeking", "seeked", "volumechange"];
  for (const event of events) {
    video.addEventListener(event, () => {
      if (!parentOrigin) return;
      const duration = Number.isFinite(video.duration) ? video.duration : null;
      window.parent.postMessage({channel: PLAYER_CHANNEL, event, value: {
        seconds: video.currentTime, duration, percent: duration ? video.currentTime / duration : 0,
        volume: video.volume, muted: video.muted,
      }}, parentOrigin);
    });
  }
}
