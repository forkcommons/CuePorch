export function requestSignInCode(form: HTMLFormElement, status: HTMLElement) {
  const field = form.querySelector<HTMLElement>("[data-sign-in-factor]")!;
  const code = field.querySelector("input")!;
  field.hidden = code.disabled = false;
  code.required = true;
  status.textContent = "Enter a code from your authenticator app or an unused recovery code.";
  code.focus();
}

export function resetSignInCode(form: HTMLFormElement) {
  const field = form.querySelector<HTMLElement>("[data-sign-in-factor]")!;
  const code = field.querySelector("input")!;
  code.value = "";
  code.required = false;
  field.hidden = code.disabled = true;
}
