(function () {
  function canTrack(): boolean {
    const dnt = navigator.doNotTrack === "1";
    const gpc = (navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl === true;
    return !dnt && !gpc;
  }
  function mount(figure: HTMLElement) {
    const frame = figure.querySelector<HTMLIFrameElement>("iframe");
    const label = figure.querySelector<HTMLLabelElement>("label[data-cueporch-consent]");
    const input = figure.querySelector<HTMLInputElement>('input[type="checkbox"]');
    const status =
      figure.querySelector<HTMLElement>("[data-cueporch-consent-status]") ??
      figure.querySelector<HTMLElement>('[role="status"]');

    let origin: string | null = null;
    try {
      origin = frame?.src ? new URL(frame.src, document.baseURI).origin : null;
    } catch {
      origin = null;
    }

    const pageId = figure.dataset.cueporchPage;
    let ready = false;
    let allowed = false;

    const render = () => {
      if (!label || !input) return;
      label.hidden = !(ready && allowed && !input.disabled);
    };

    const probe = () => {
      const win = frame?.contentWindow;
      if (!win || !origin) return;
      win.postMessage({ type: "cueporch-hubspot-probe" }, origin);
    };

    window.addEventListener("message", (event: MessageEvent) => {
      if (!frame || !origin) return;
      if (event.origin !== origin || event.source !== frame.contentWindow) return;

      const data = event.data as { type?: unknown } | null;
      if (!data || typeof data !== "object") return;

      if (data.type === "cueporch-hubspot-ready") {
        ready = true;
        render();
      } else if (data.type === "cueporch-hubspot-active") {
        if (status) status.textContent = "Playback sharing active.";
      } else if (data.type === "cueporch-hubspot-stopped") {
        if (input) {
          input.disabled = true;
          input.checked = false;
        }
        if (label) label.hidden = true;
        if (status) status.textContent = "Playback sharing stopped.";
      } else if (data.type === "cueporch-hubspot-unavailable") {
        if (status) status.textContent = "Playback sharing unavailable.";
      }
    });

    input?.addEventListener("change", () => {
      if (!input) return;

      const win = frame?.contentWindow;
      if (!win || !origin) return;

      if (!input.checked) {
        win.postMessage({ type: "cueporch-hubspot-stop" }, origin);
        return;
      }

      if (!allowed || !canTrack()) {
        input.checked = false;
        return;
      }

      const match = document.cookie.match(/(?:^|;\s*)hubspotutk=([0-9a-f]{32})(?:;|$)/i);
      const contactUtk = match?.[1];

      if (!contactUtk) {
        input.checked = false;
        if (status) status.textContent = "Playback sharing unavailable.";
        return;
      }

      win.postMessage({ type: "cueporch-hubspot-share", contactUtk, pageId }, origin);
    });

    const w = window as Window & { _hsp?: unknown[] };
    w._hsp ??= [];
    w._hsp.push([
      "addPrivacyConsentListener",
      (consent: { allowed?: boolean; categories?: { analytics?: boolean } }) => {
        const nextAllowed =
          ((consent?.categories?.analytics ?? consent?.allowed) === true) && canTrack();
        const wasChecked = input?.checked === true;
        allowed = nextAllowed;

        if (!allowed) {
          if (input) input.checked = false;
          const win = frame?.contentWindow;
          if (wasChecked && win && origin) {
            win.postMessage({ type: "cueporch-hubspot-stop" }, origin);
          }
        }

        render();
      },
    ]);

    probe();
    frame?.addEventListener("load", probe);
    render();
  }
  function boot(): void {
    document.querySelectorAll<HTMLElement>("figure[data-cueporch-page]").forEach(mount);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
