export function waitingPlayer(scope) {
    const panel = scope.querySelector("[data-event-waiting]");
    const poster = panel.querySelector("[data-event-poster]");
    const next = panel.querySelector("[data-event-next]");
    const label = next.querySelector("span");
    const time = next.querySelector("time");
    const recurring = panel.querySelector("[data-event-repeat]");
    return (value) => {
        panel.hidden = !value;
        poster.hidden = !value?.poster;
        if (value?.poster) {
            if (poster.getAttribute("src") !== value.poster)
                poster.src = value.poster;
        }
        else
            poster.removeAttribute("src");
        next.hidden = !value?.message;
        label.hidden = !value?.date;
        if (value?.date)
            time.dateTime = value.date;
        else
            time.removeAttribute("datetime");
        time.textContent = value?.message || "";
        recurring.hidden = !value?.recurring;
        recurring.textContent = value?.recurring || "";
    };
}
