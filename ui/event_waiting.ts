export interface BeforeEvent {
  poster: string | null;
  date: string;
  message: string;
  recurring: string;
}

export function waitingPlayer(scope: ParentNode) {
  const panel = scope.querySelector<HTMLElement>("[data-event-waiting]")!;
  const poster = panel.querySelector<HTMLImageElement>("[data-event-poster]")!;
  const next = panel.querySelector<HTMLElement>("[data-event-next]")!;
  const label = next.querySelector("span")!;
  const time = next.querySelector("time")!;
  const recurring = panel.querySelector<HTMLElement>("[data-event-repeat]")!;
  return (value: BeforeEvent | null) => {
    panel.hidden = !value;
    poster.hidden = !value?.poster;
    if (value?.poster) {
      if (poster.getAttribute("src") !== value.poster) poster.src = value.poster;
    } else poster.removeAttribute("src");
    next.hidden = !value?.message;
    label.hidden = !value?.date;
    if (value?.date) time.dateTime = value.date;
    else time.removeAttribute("datetime");
    time.textContent = value?.message || "";
    recurring.hidden = !value?.recurring;
    recurring.textContent = value?.recurring || "";
  };
}
