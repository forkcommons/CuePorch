import { playbackObservation } from "./playback_analytics.js";

function bind(video: HTMLVideoElement, origin: string, analytics: string): void {
  const endpoint = analytics + "/hubspot";

  let started = false;
  let withdrawn = false;
  let dispose: ((report?: boolean) => void) | undefined;
  let queue: Promise<void> = Promise.resolve();

  function reply(
    type:
      | "cueporch-hubspot-ready"
      | "cueporch-hubspot-active"
      | "cueporch-hubspot-stopped"
      | "cueporch-hubspot-unavailable"
  ): void {
    window.parent.postMessage({ type }, origin);
  }

  function onMessage(event: MessageEvent): void {
    if (event.origin !== origin) return;
    if (event.source !== window.parent) return;

    const data = event.data as Record<string, unknown> | null;
    if (typeof data !== "object" || data === null) return;

    const type = data.type;
    if (type === "cueporch-hubspot-probe") {
      window.parent.postMessage({ type: "cueporch-hubspot-ready" }, origin);
      return;
    }

    if (type === "cueporch-hubspot-share" || type === "cueporch-hubspot-stop") {
      queue = queue
        .then(async () => {
          if (type === "cueporch-hubspot-share") {
            if (started || withdrawn) return;

            const contactUtk = data.contactUtk;
            const pageId = data.pageId;
            if (typeof contactUtk !== "string" || typeof pageId !== "string") {
              reply("cueporch-hubspot-unavailable");
              return;
            }

            const response = await fetch(endpoint, {
              method: "PUT",
              headers: {
                "Content-Type": "application/json",
                "X-CuePorch-HubSpot-View": "1"
              },
              body: JSON.stringify({
                consent: true,
                contactUtk,
                pageId
              }),
              keepalive: true
            });

            if (!response.ok) {
              reply("cueporch-hubspot-unavailable");
              return;
            }

            dispose = playbackObservation(endpoint, true, true)(video, true);
            started = true;
            reply("cueporch-hubspot-active");
          } else {
            dispose?.(false);
            dispose = undefined;
            withdrawn = true;

            const response = await fetch(endpoint, {
              method: "DELETE",
              headers: {
                "Content-Type": "application/json",
                "X-CuePorch-HubSpot-View": "1"
              },
              body: JSON.stringify({}),
              keepalive: true
            });

            if (!response.ok) {
              reply("cueporch-hubspot-unavailable");
              return;
            }

            reply("cueporch-hubspot-stopped");
          }
        })
        .catch(() => {
          reply("cueporch-hubspot-unavailable");
        });
    }
  }

  window.addEventListener("message", onMessage);
}

const video = document.querySelector<HTMLVideoElement>(
  "video[data-hubspot-origin][data-analytics]"
);
if (video && window.parent !== window) {
  const { hubspotOrigin, analytics } = video.dataset;
  if (typeof hubspotOrigin === "string" && typeof analytics === "string") {
    bind(video, hubspotOrigin, analytics);
  }
}
