import { viewerRequest } from "./audience_api.js";
import { teamRequest } from "./team_api.js";
import { requestSignInCode } from "./sign_in_factor.js";
const main = document.querySelector("#passwordRecovery");
const status = document.querySelector("#passwordRecoveryStatus");
const requestLink = document.querySelector("#requestPasswordReset");
const change = document.querySelector("#choosePassword");
const send = main.dataset.scope === "team" ? teamRequest : viewerRequest;
const fragment = location.hash.slice(1);
const token = /^[A-Za-z0-9_-]{43}$/.test(fragment) ? fragment : "";
if (fragment)
    history.replaceState(null, "", location.pathname);
requestLink.hidden = Boolean(token) || main.dataset.mail !== "yes";
change.hidden = !token;
if (requestLink.hidden && change.hidden)
    status.textContent = "This installation has not configured password reset email.";
async function submit(form, action) {
    const button = form.querySelector("button");
    if (button.disabled)
        return;
    button.disabled = true;
    status.textContent = "";
    try {
        await action();
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        button.disabled = false;
    }
}
async function checked(response) {
    if (response.ok)
        return;
    const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
    throw new Error(detail || "CuePorch could not complete the reset.");
}
requestLink.onsubmit = event => {
    event.preventDefault();
    void submit(requestLink, async () => {
        await checked(await send("password-recovery", "POST", Object.fromEntries(new FormData(requestLink))));
        status.textContent = "If this address belongs to a verified account, CuePorch will send a reset link.";
    });
};
change.onsubmit = event => {
    event.preventDefault();
    void submit(change, async () => {
        const values = new FormData(change);
        if (values.get("password") !== values.get("confirm"))
            throw new Error("The passwords do not match.");
        const response = await send("password-recovery", "PUT", {
            reset_token: token, password: values.get("password"), code: values.get("code"),
        });
        if (response.status === 428)
            return requestSignInCode(change, status);
        await checked(response);
        change.reset();
        change.hidden = true;
        status.textContent = "Password changed. Return to sign in.";
    });
};
