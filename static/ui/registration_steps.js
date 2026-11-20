const form = document.querySelector("[data-registration-steps]");
const identity = form.querySelector("[data-identity]");
const questions = form.querySelector("[data-questions]");
const controls = form.querySelector("[data-step-controls]");
const stepOne = controls.querySelector("[data-step-one]");
const stepTwo = controls.querySelector("[data-step-two]");
const back = controls.querySelector("[data-previous]");
const next = controls.querySelector("[data-next]");
const submit = form.querySelector("button[type=submit]");
function show(first) {
    identity.hidden = !first;
    questions.hidden = first;
    back.hidden = submit.hidden = first;
    next.hidden = !first;
    stepOne.hidden = !first;
    stepTwo.hidden = first;
}
function advance() {
    const fields = [...identity.querySelectorAll("input")];
    if (fields.every((field) => field.reportValidity())) {
        show(false);
        (questions.querySelector("input,select") || submit).focus();
    }
}
next.onclick = advance;
back.onclick = () => { show(true); identity.querySelector("input")?.focus(); };
identity.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        event.preventDefault();
        advance();
    }
});
show(true);
controls.hidden = false;
export {};
