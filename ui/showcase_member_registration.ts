export type ShowcaseRegistrationItem = {
  id: string;
  registration?: {
    timing: "before" | "during" | "after";
    time_seconds: number;
    url: string;
  };
};

export function createShowcaseRegistration<T extends ShowcaseRegistrationItem>() {
  const panel = document.querySelector<HTMLElement>("#showcaseMemberRegistration");
  const frame =
    panel?.querySelector<HTMLIFrameElement>('iframe[title="Video registration"]') ?? null;
  const dismiss =
    panel?.querySelector<HTMLButtonElement>("[data-member-registration-dismiss]") ?? null;

  const seen = new Set<string>();

  type Active = {
    itemId: string;
    video: HTMLVideoElement;
    resume: boolean;
    advance?: () => void;
    unlock?: (entry: T) => void;
    trigger?: HTMLElement | null;
  };

  let active: Active | null = null;

  const hide = (): Active | null => {
    const prior = active;
    if (!prior) return null;
    active = null;
    if (frame) frame.src = "about:blank";
    if (panel) panel.hidden = true;
    return prior;
  };

  const guardPlay = (video: HTMLVideoElement): boolean => {
    if (active?.video !== video) return false;
    video.pause();
    return true;
  };

  const show = (
    item: T,
    video: HTMLVideoElement,
    advance?: () => void,
    unlock?: (entry: T) => void,
    trigger?: HTMLElement | null
  ): boolean => {
    const registration = item.registration;
    if (!registration || !panel || !frame || active || seen.has(item.id)) {
      return false;
    }

    const resume = !video.paused;
    if (resume) video.pause();

    active = { itemId: item.id, video, resume, advance, unlock, trigger };
    frame.src = registration.url;
    panel.hidden = false;
    frame.focus();
    return true;
  };

  const complete = () => {
    const prior = hide();
    if (!prior) return;
    seen.add(prior.itemId);
    prior.video.focus();
    if (prior.resume) {
      void prior.video.play().catch(() => {});
    } else {
      prior.advance?.();
    }
  };

  const closeBefore = () => {
    const prior = hide();
    if (!prior) return;
    prior.trigger?.focus();
  };

  dismiss?.addEventListener("click", (event) => {
    if (!event.isTrusted) return;
    if (active?.unlock) {
      closeBefore();
    } else {
      complete();
    }
  });

  window.addEventListener("message", (event) => {
    if (!active || !frame) return;
    if (!event.isTrusted) return;
    if (event.origin !== location.origin) return;
    if (event.source !== frame.contentWindow) return;

    if (event.data === "cueporch:member-registered") {
      if (!active.unlock) complete();
      return;
    }

    const data = event.data as {
      type?: unknown;
      entry?: { id?: unknown; media?: unknown };
    } | null;
    if (data?.type !== "cueporch:member-registered" || !data.entry) return;
    if (!active.unlock) return;
    if (data.entry.id !== active.itemId) return;
    if (typeof data.entry.media !== "string") return;

    const prior = hide();
    prior?.unlock?.(data.entry as T);
  });

  return {
    guardPlay,

    onProgress(item: T, video: HTMLVideoElement): void {
      const registration = item.registration;
      if (!registration || registration.timing !== "during") return;
      if (seen.has(item.id) || active) return;

      const duration = video.duration;
      if (!Number.isFinite(duration) || duration <= 0) return;

      const time = registration.time_seconds;
      if (!Number.isFinite(time) || time <= 0) return;

      const threshold = Math.min(duration, time);
      if (threshold <= 0 || video.currentTime < threshold) return;

      show(item, video);
    },

    onEnd(
      item: T,
      video: HTMLVideoElement,
      advance: () => void
    ): boolean {
      if (active?.video === video && active.itemId === item.id) {
        active.resume = false;
        active.advance = advance;
        return true;
      }

      if (item.registration?.timing !== "after") return false;
      return show(item, video, advance);
    },

    before(
      item: T,
      video: HTMLVideoElement,
      trigger: HTMLElement | null,
      onUnlock: (entry: T) => void
    ): boolean {
      if (item.registration?.timing !== "before") return false;
      return show(item, video, undefined, onUnlock, trigger);
    },

    closeForSelection(): void {
      hide();
    },
  };
}
