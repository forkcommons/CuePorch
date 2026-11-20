import { viewerRequest } from "./audience_api.js";
import { teamRequest } from "./team_api.js";
const security = document.querySelector("#accountSecurity");
const status = document.querySelector("#authenticatorStatus");
const begin = document.querySelector("#authenticatorStart");
const confirmation = document.querySelector("#authenticatorConfirm");
const disable = document.querySelector("#authenticatorDisable");
const recovery = document.querySelector("#authenticatorRecovery");
const seed = document.querySelector("#authenticatorSeed");
const openApp = document.querySelector("#authenticatorOpen");
const emailSection = document.querySelector("#verifyAccountEmail");
const emailAddress = document.querySelector("#verifiedAccountAddress");
const emailStatus = document.querySelector("#verifyAccountEmailStatus");
const sendCode = document.querySelector("#sendVerificationCode");
const confirmEmail = document.querySelector("#confirmEmailCode");
const factorSection = document.querySelector("#emailFactor");
const factorStatus = document.querySelector("#emailFactorStatus");
const factorEnable = document.querySelector("#emailFactorEnable");
const factorDisable = document.querySelector("#emailFactorDisable");
let setupTicket = "";
let emailTicket = "";
let appEnabled = false;
let busy = false;
async function request(method, body = {}, path = "authenticator") {
    const send = security.dataset.scope === "team" ? teamRequest : viewerRequest;
    const response = await send(path, method, body);
    if (response.ok)
        return response.json();
    const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
    if (method === "GET" && response.status === 401)
        throw new Error("Sign in to manage your account security.");
    throw new Error(detail || "CuePorch could not complete the security request.");
}
async function perform(action, feedback = status) {
    if (busy)
        return;
    busy = true;
    security.setAttribute("aria-busy", "true");
    try {
        await action();
    }
    catch (error) {
        feedback.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        busy = false;
        security.removeAttribute("aria-busy");
    }
}
function disposeSetup() {
    setupTicket = "";
    confirmation.reset();
    seed.value = "";
    openApp.removeAttribute("href");
    confirmation.hidden = true;
}
function state(enabled) {
    appEnabled = enabled;
    disposeSetup();
    begin.hidden = enabled;
    disable.hidden = !enabled;
    status.textContent = enabled ? "Authenticator sign-in is on." : "Authenticator sign-in is off.";
}
function bind(form, action, feedback = status) {
    form.addEventListener("submit", event => {
        event.preventDefault();
        const values = Object.fromEntries(new FormData(form));
        form.reset();
        void perform(() => action(values), feedback);
    });
}
bind(begin, async (values) => {
    const prepared = await request("POST", values);
    setupTicket = prepared.setup_ticket;
    seed.value = prepared.setup_key;
    openApp.href = prepared.app_link;
    begin.hidden = true;
    confirmation.hidden = false;
    status.textContent = "Enter a code from the app to confirm setup.";
    seed.focus();
});
bind(confirmation, async (values) => {
    const enabled = await request("PUT", { ...values, setup_ticket: setupTicket });
    state(true);
    showRecoveryCodes(enabled.recovery_codes);
    await readEmailFactor();
});
function showRecoveryCodes(codes = []) {
    const list = recovery.querySelector("ul");
    list.replaceChildren(...codes.map(code => {
        const item = document.createElement("li");
        item.textContent = code;
        return item;
    }));
    recovery.hidden = codes.length === 0;
}
bind(disable, async (values) => {
    await request("DELETE", values);
    showRecoveryCodes();
    state(false);
    await readEmailFactor();
});
document.querySelector("#authenticatorCancel").onclick = () => {
    if (!busy)
        state(false);
};
function emailState(address, message, canSend, canConfirm) {
    emailAddress.textContent = address;
    emailStatus.textContent = message;
    sendCode.hidden = !canSend;
    confirmEmail.hidden = !canConfirm;
    emailSection.hidden = false;
}
sendCode.onclick = () => void perform(async () => {
    const result = await request("POST", {}, "email-verification");
    if (result.verified)
        emailState(result.address, "Email verified.", false, false);
    else {
        emailState(result.address, "The relay accepted your code. Check your email.", true, true);
        confirmEmail.querySelector("input").focus();
    }
}, emailStatus);
bind(confirmEmail, async (values) => {
    const result = await request("PUT", values, "email-verification");
    emailState(result.address, "Email verified.", false, false);
    await readEmailFactor();
}, emailStatus);
async function readEmailFactor() {
    const factor = await request("GET", {}, "email-factor");
    emailTicket = "";
    begin.hidden = appEnabled || factor.enabled;
    factorEnable.hidden = factor.enabled || appEnabled || !factor.available || !factor.verified;
    factorDisable.hidden = !factor.enabled;
    factorStatus.textContent = factor.enabled ? "Email sign-in codes are on."
        : appEnabled ? "Turn off the authenticator app to switch to email sign-in codes."
            : !factor.available ? "Email sign-in codes are unavailable on this installation."
                : !factor.verified ? "Verify your email to use email sign-in codes." : "Email sign-in codes are off.";
    factorSection.hidden = false;
}
bind(factorEnable, async (values) => {
    const result = await request("PUT", values, "email-factor");
    showRecoveryCodes(result.recovery_codes);
    await readEmailFactor();
}, factorStatus);
bind(factorDisable, async (values) => {
    await request("DELETE", { ...values, email_ticket: emailTicket }, "email-factor");
    showRecoveryCodes();
    await readEmailFactor();
}, factorStatus);
document.querySelector("#emailFactorSend").onclick = () => void perform(async () => {
    const password = factorDisable.elements.namedItem("password");
    if (!password.reportValidity())
        return;
    const result = await request("POST", { password: password.value }, "email-factor/code");
    if (result.email_ticket)
        emailTicket = result.email_ticket;
    factorStatus.textContent = result.sent ? "Check your email for the security code."
        : "Use the last code you received, wait before sending another, or enter an unused recovery code.";
    factorDisable.elements.namedItem("code").focus();
}, factorStatus);
void perform(async () => {
    state((await request("GET")).enabled);
    const result = await request("GET", {}, "email-verification");
    const message = result.verified ? "Email verified." : result.pending ? "Enter the verification code from your email."
        : result.available ? "Email is awaiting verification." : "Email delivery is unavailable on this installation.";
    emailState(result.address, message, !result.verified && result.available, result.pending);
    await readEmailFactor();
});
