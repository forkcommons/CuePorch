interface Step {anchor: HTMLElement; title: string; instruction: string}

for (const tour of document.querySelectorAll<HTMLElement>(".workspace-tour")) {
  const toggle = tour.querySelector<HTMLButtonElement>("[data-tour-toggle]")!;
  const panel = tour.querySelector<HTMLElement>(".workspace-tour-mark")!;
  const template = tour.querySelector<HTMLTemplateElement>("[data-tour-steps]")!;
  const heading = panel.querySelector<HTMLElement>("[data-tour-heading]")!;
  const progress = panel.querySelector<HTMLElement>("[data-tour-progress]")!;
  const instruction = panel.querySelector<HTMLElement>("[data-tour-instruction]")!;
  const back = panel.querySelector<HTMLButtonElement>("[data-tour-back]")!;
  const next = panel.querySelector<HTMLButtonElement>("[data-tour-next]")!;
  const dismiss = panel.querySelector<HTMLButtonElement>("[data-tour-close]")!;
  const scope = tour.closest("main")!;
  let steps: Step[] = [];
  let position = 0;
  let highlighted: HTMLElement | undefined;

  function show() {
    highlighted?.classList.remove("workspace-tour-highlight");
    const {anchor, title, instruction: text} = steps[position];
    highlighted = anchor;
    anchor.classList.add("workspace-tour-highlight");
    heading.textContent = title;
    instruction.textContent = text;
    progress.textContent = `Step ${position + 1} of ${steps.length}`;
    back.disabled = position === 0;
    next.textContent = position + 1 === steps.length ? "Finish" : "Next";
    anchor.insertAdjacentElement("afterend", panel);
    panel.hidden = false;
    panel.scrollIntoView({block: "nearest"});
    heading.focus({preventScroll: true});
  }

  function close() {
    highlighted?.classList.remove("workspace-tour-highlight");
    highlighted = undefined;
    panel.hidden = true;
    tour.append(panel);
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "Take a tour";
    toggle.focus();
  }

  toggle.onclick = () => {
    if (!panel.hidden) {
      close();
      return;
    }
    steps = [];
    for (const item of template.content.querySelectorAll<HTMLElement>("[data-tour-anchor]")) {
      const anchor = scope.querySelector<HTMLElement>(item.dataset.tourAnchor!);
      if (anchor?.getClientRects().length) {
        steps.push({anchor, title: item.dataset.tourTitle!, instruction: item.textContent});
      }
    }
    if (!steps.length) return;
    position = 0;
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = "Close tour";
    show();
  };
  back.onclick = () => {
    position -= 1;
    show();
  };
  next.onclick = () => {
    if (position + 1 === steps.length) close();
    else {
      position += 1;
      show();
    }
  };
  dismiss.onclick = close;
  panel.onkeydown = event => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  };
}
