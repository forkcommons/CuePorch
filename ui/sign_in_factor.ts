interface Challenge {method?: "email"; email_ticket?: string; sent?: boolean}

export function requestSignInCode(form: HTMLFormElement, status: HTMLElement, challenge: Challenge = {}) {
  const field = form.querySelector<HTMLElement>("[data-sign-in-factor]")!;
  const code = field.querySelector("input")!;
  const resend = form.querySelector<HTMLButtonElement>("[data-send-email-factor]");
  const mailed = challenge.method === "email";
  if (resend) resend.hidden = !mailed;
  if (challenge.email_ticket) form.dataset.emailTicket = challenge.email_ticket;
  field.hidden = code.disabled = false;
  code.required = true;
  field.querySelector("span")!.textContent = mailed ? "Email security code or recovery code" : "Authenticator or recovery code";
  status.textContent = mailed
    ? challenge.sent ? "Check your email for a security code, or enter an unused recovery code."
      : "Use the last code you received, wait before sending another, or enter an unused recovery code."
    : "Enter a code from your authenticator app or an unused recovery code.";
  if (challenge.sent) code.value = "";
  code.focus();
}

export function signInValues(form: HTMLFormElement, submitter: HTMLElement | null) {
  return {...Object.fromEntries(new FormData(form)), email_ticket: form.dataset.emailTicket,
    resend: submitter?.hasAttribute("data-send-email-factor") || undefined};
}

export function resetSignInCode(form: HTMLFormElement) {
  const field = form.querySelector<HTMLElement>("[data-sign-in-factor]")!;
  const code = field.querySelector("input")!;
  code.value = "";
  code.required = false;
  field.hidden = code.disabled = true;
  const resend = form.querySelector<HTMLButtonElement>("[data-send-email-factor]");
  if (resend) resend.hidden = true;
  delete form.dataset.emailTicket;
}
