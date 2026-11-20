import { viewerRequest } from "./audience_api.js";
import { requestSignInCode, resetSignInCode } from "./sign_in_factor.js";
const top = document.querySelector("#viewerAccount");
const status = document.querySelector("#viewerAccountStatus");
const entrance = document.querySelector("#viewerEntrance");
const management = document.querySelector("#viewerManagement");
const signIn = document.querySelector("#viewerSignIn");
const signUp = document.querySelector("#viewerSignUp");
const details = document.querySelector("#viewerDetails");
const change = document.querySelector("#changeViewerPassword");
const remove = document.querySelector("#deleteViewerAccount");
const signOut = document.querySelector("#viewerSignOut");
const preferenceSection = document.querySelector("#viewerPreferences");
const preferenceForm = preferenceSection.querySelector("form");
const preferenceMode = preferenceForm.querySelector("select");
const preferenceChoices = preferenceForm.querySelector("fieldset");
let busy = false;
function show(account) {
    entrance.hidden = !!account;
    management.hidden = !account;
    if (!account) {
        preferenceSection.hidden = true;
        preferenceForm.reset();
        resetSignInCode(signIn);
        for (const form of [signIn, signUp, details, change, remove])
            form.reset();
        return;
    }
    for (const key of ["name", "email", "pronouns"]) {
        details.elements.namedItem(key).value = account[key];
    }
}
async function perform(run) {
    if (busy)
        return;
    busy = true;
    top.setAttribute("aria-busy", "true");
    status.textContent = "";
    try {
        await run();
    }
    catch {
        status.textContent = "Could not contact CuePorch. Try again.";
    }
    finally {
        busy = false;
        top.removeAttribute("aria-busy");
    }
}
function failure(response, messages) {
    status.textContent = messages[response.status] || "CuePorch could not complete the request.";
}
const messages = { 400: "Check the fields and try again.", 429: "Too many attempts. Try again later." };
const values = (form) => Object.fromEntries(new FormData(form));
function showPreferences(preferences) {
    preferenceMode.value = preferences.mode;
    for (const choice of preferenceChoices.querySelectorAll("input")) {
        choice.checked = preferences.excluded.includes(choice.value);
    }
    preferenceChoices.hidden = preferences.mode !== "custom";
    preferenceSection.hidden = false;
}
async function showSignedIn(account) {
    show(account);
    const response = await viewerRequest("preferences");
    if (response.ok)
        showPreferences(await response.json());
    else
        status.textContent = "Could not load viewing preferences. Reload to try again.";
}
preferenceMode.onchange = () => {
    preferenceChoices.hidden = preferenceMode.value !== "custom";
};
preferenceForm.onsubmit = event => {
    event.preventDefault();
    void perform(async () => {
        const response = await viewerRequest("preferences", "PUT", {
            mode: preferenceMode.value, excluded: new FormData(preferenceForm).getAll("excluded"),
        });
        if (!response.ok)
            return failure(response, { ...messages, 401: "Sign in to save viewing preferences." });
        showPreferences(await response.json());
        status.textContent = "Viewing preferences saved.";
    });
};
function connect(form, method) {
    form.onsubmit = event => {
        event.preventDefault();
        void perform(async () => {
            const response = await viewerRequest(method === "sign in" ? "session" : "signup", "POST", values(form));
            if (response.status === 428 && method === "sign in")
                return requestSignInCode(form, status);
            if (!response.ok)
                return failure(response, {
                    ...messages, 401: "The email or password did not match.", 409: "That email address is already in use.",
                    422: "That code did not match or has already been used.",
                });
            form.reset();
            if (method === "sign in")
                resetSignInCode(form);
            if (top.dataset.destination)
                location.assign(top.dataset.destination);
            else
                await showSignedIn(await response.json());
        });
    };
}
connect(signIn, "sign in");
connect(signUp, "sign up");
signIn.querySelector("input[type=email]").oninput = () => resetSignInCode(signIn);
details.onsubmit = event => {
    event.preventDefault();
    void perform(async () => {
        const response = await viewerRequest("account", "PUT", values(details));
        details.elements.namedItem("current_password").value = "";
        if (!response.ok)
            return failure(response, {
                ...messages, 400: "Check the fields. Enter your current password to change email.",
                401: "Check your current password or sign in again.", 409: "The email may be in use. Reload and try again.",
            });
        show(await response.json());
        status.textContent = "Account details saved.";
    });
};
change.onsubmit = event => {
    event.preventDefault();
    void perform(async () => {
        const response = await viewerRequest("password", "PUT", values(change));
        change.reset();
        if (!response.ok)
            return failure(response, { ...messages, 401: "Check your current password or sign in again." });
        status.textContent = "Password changed. Your other sessions were signed out.";
    });
};
remove.onsubmit = event => {
    event.preventDefault();
    void perform(async () => {
        const response = await viewerRequest("account", "DELETE", values(remove));
        remove.reset();
        if (!response.ok)
            return failure(response, { ...messages, 401: "Check your current password or sign in again." });
        show();
        status.textContent = "Account deleted.";
    });
};
signOut.onclick = () => {
    void perform(async () => {
        const response = await viewerRequest("session", "DELETE");
        if (!response.ok)
            return failure(response, {});
        show();
        status.textContent = "Signed out.";
    });
};
void perform(async () => {
    const response = await viewerRequest("account");
    if (response.ok)
        await showSignedIn(await response.json());
    else if (response.status === 401)
        show();
    else
        failure(response, {});
});
