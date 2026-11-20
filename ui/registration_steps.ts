export {};

const form = document.querySelector<HTMLFormElement>("[data-registration-steps]")!;
const identity = form.querySelector<HTMLElement>("[data-identity]")!;
const questions = form.querySelector<HTMLElement>("[data-questions]")!;
const controls = form.querySelector<HTMLElement>("[data-step-controls]")!;
const stepOne = controls.querySelector<HTMLElement>("[data-step-one]")!;
const stepTwo = controls.querySelector<HTMLElement>("[data-step-two]")!;
const back = controls.querySelector<HTMLButtonElement>("[data-previous]")!;
const next = controls.querySelector<HTMLButtonElement>("[data-next]")!;
const submit = form.querySelector<HTMLButtonElement>("button[type=submit]")!;

function show(first: boolean) {
  identity.hidden = !first;
  questions.hidden = first;
  back.hidden = submit.hidden = first;
  next.hidden = !first;
  stepOne.hidden = !first;
  stepTwo.hidden = first;
}

function advance() {
  const fields = [...identity.querySelectorAll<HTMLInputElement>("input")];
  if (fields.every((field) => field.reportValidity())) {
    show(false);
    (questions.querySelector<HTMLElement>("input,select") || submit).focus();
  }
}

next.onclick = advance;
back.onclick = () => { show(true); identity.querySelector<HTMLInputElement>("input")?.focus(); };
identity.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); advance(); }
});
show(true);
controls.hidden = false;
