type Request = <T>(path: string, method?: string, body?: object) => Promise<T>;

interface WebinarEmail {
  ready: boolean;
  revision: string | null;
  settings: {confirmation: boolean; reminder_minutes: number[]; follow_up_hours: number | null};
  deliveries: Partial<Record<"queued" | "accepted" | "failed" | "canceled", number>>;
}

export function setupEventMail(request: Request) {
  const panel = document.querySelector<HTMLDetailsElement>("#eventMailSettings")!;
  const form = document.querySelector<HTMLFormElement>("#eventMailForm")!;
  const fields = form.querySelector("fieldset")!;
  const relay = document.querySelector("#eventMailRelay")!;
  const deliveries = document.querySelector("#eventMailDeliveries")!;
  const status = document.querySelector("#eventMailStatus")!;
  const confirmation = form.elements.namedItem("confirmation") as HTMLInputElement;
  const reminders = form.elements.namedItem("reminders") as HTMLInputElement;
  const followUp = form.elements.namedItem("followUp") as HTMLInputElement;
  let event: string | undefined;
  let revision: string | null;
  let generation = 0;

  async function update(method = "GET", body?: object) {
    if (!event) return;
    const ticket = ++generation;
    fields.disabled = true;
    status.textContent = "";
    try {
      const result = await request<WebinarEmail>(`/api/live-events/${event}/registration/email`, method, body);
      if (ticket !== generation) return;
      revision = result.revision;
      confirmation.checked = result.settings.confirmation;
      reminders.value = result.settings.reminder_minutes.join(", ");
      followUp.value = result.settings.follow_up_hours?.toString() ?? "";
      relay.textContent = result.ready ? "The secure workspace mail relay is configured."
        : "Configure the public HTTPS address and secure mail relay before enabling emails.";
      const {queued = 0, accepted = 0, failed = 0, canceled = 0} = result.deliveries;
      deliveries.textContent = `${queued} queued, ${accepted} accepted by the relay, ${failed} failed, ${canceled} canceled.`;
      fields.disabled = false;
      if (method === "PUT") status.textContent = "Webinar email settings saved.";
    } catch (error) {
      if (ticket !== generation) return;
      fields.disabled = method === "GET";
      status.textContent = (error as Error).message;
    }
  }

  panel.addEventListener("toggle", () => {
    if (panel.open) void update();
  });
  form.addEventListener("submit", evt => {
    evt.preventDefault();
    if (fields.disabled) return;
    const values = reminders.value.trim() ? reminders.value.split(",").map(value => value.trim()) : [];
    if (values.some(value => !/^\d+$/.test(value))) {
      status.textContent = "Enter each reminder as a whole number of minutes.";
      return;
    }
    void update("PUT", {revision, settings: {confirmation: confirmation.checked,
      reminder_minutes: values.map(Number), follow_up_hours: followUp.value ? Number(followUp.value) : null}});
  });

  return (next: string) => {
    if (event === next) return;
    event = next;
    generation++;
    panel.open = false;
    fields.disabled = true;
    relay.textContent = "";
    deliveries.textContent = "";
    status.textContent = "";
  };
}
