import { viewerRequest } from "./audience_api.js";
import { requestSignInCode, resetSignInCode, signInValues } from "./sign_in_factor.js";
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
const preferenceMode = preferenceForm.querySelector("select[name=mode]");
const preferenceAutoplay = preferenceForm.querySelector("select[name=autoplay]");
const preferenceLanguage = preferenceForm.querySelector("select[name=language]");
const preferenceChoices = preferenceForm.querySelector("fieldset");
const likeSharingSection = document.querySelector("#viewerLikeSharing");
const likeSharingInfo = document.querySelector("#viewerLikeSharingInfo");
const likeSharingToggle = document.querySelector("#viewerLikeSharingToggle");
let likeSharing = null;
let busy = false;
function show(account) {
    entrance.hidden = !!account;
    management.hidden = !account;
    if (!account) {
        preferenceSection.hidden = true;
        preferenceForm.reset();
        showLikeSharing(null);
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
    preferenceAutoplay.value = preferences.autoplay === true ? "on" : preferences.autoplay === false ? "off" : "default";
    preferenceLanguage.value = preferences.language ?? "";
    preferenceLanguage.dataset.saved = preferenceLanguage.value;
    for (const choice of preferenceChoices.querySelectorAll("input")) {
        choice.checked = preferences.excluded.includes(choice.value);
    }
    preferenceChoices.hidden = preferences.mode !== "custom";
    preferenceSection.hidden = false;
}
function showLikeSharing(next) {
    likeSharing = next;
    const profile = next?.enabled ? next.viewer : next?.candidate;
    likeSharingSection.hidden = !profile;
    if (!next || !profile)
        return;
    likeSharingInfo.textContent = next.enabled
        ? `New public likes are shared with followers of ${profile.name}.`
        : `Share only new public likes with followers of ${profile.name}.`;
    likeSharingToggle.textContent = next.enabled ? "Stop sharing likes" : "Share new likes";
}
async function showSignedIn(account) {
    show(account);
    const [preferences, sharing] = await Promise.all([viewerRequest("preferences"), viewerRequest("like-sharing")]);
    if (preferences.ok) {
        const saved = await preferences.json();
        if (saved.language && saved.language !== top.dataset.language) {
            location.reload();
            return;
        }
        showPreferences(saved);
    }
    else
        status.textContent = "Could not load viewing preferences. Reload to try again.";
    if (sharing.ok)
        showLikeSharing(await sharing.json());
}
likeSharingToggle.onclick = () => {
    void perform(async () => {
        if (!likeSharing)
            return;
        const enabled = !likeSharing.enabled;
        const response = await viewerRequest("like-sharing", "PUT", { enabled, handle: likeSharing.candidate?.handle });
        if (!response.ok)
            return failure(response, {
                401: "Sign in to change like sharing.",
                409: "Check your creator sign-in and whether that profile already shares another account's likes.",
            });
        showLikeSharing(await response.json());
        status.textContent = enabled ? "New likes will be shared." : "Like sharing turned off.";
    });
};
preferenceMode.onchange = () => {
    preferenceChoices.hidden = preferenceMode.value !== "custom";
};
preferenceForm.onsubmit = event => {
    event.preventDefault();
    void perform(async () => {
        const before = preferenceLanguage.dataset.saved;
        const response = await viewerRequest("preferences", "PUT", {
            mode: preferenceMode.value, excluded: new FormData(preferenceForm).getAll("excluded"),
            autoplay: preferenceAutoplay.value === "default" ? null : preferenceAutoplay.value === "on",
            language: preferenceLanguage.value || null,
        });
        if (!response.ok)
            return failure(response, { ...messages, 401: "Sign in to save viewing preferences." });
        showPreferences(await response.json());
        status.textContent = "Viewing preferences saved.";
        if (preferenceLanguage.value !== before)
            location.reload();
    });
};
function connect(form, method) {
    form.onsubmit = event => {
        event.preventDefault();
        void perform(async () => {
            const body = method === "sign in" ? signInValues(form, event.submitter) : values(form);
            const response = await viewerRequest(method === "sign in" ? "session" : "signup", "POST", body);
            if (response.status === 428 && method === "sign in")
                return requestSignInCode(form, status, await response.json());
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
        if (top.dataset.language !== top.dataset.browserLanguage)
            location.reload();
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
