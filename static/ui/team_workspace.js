import { uploadLibraryFile } from "./library_upload.js";
import { checked, roleLabels, sessionMarker, teamRequest } from "./team_api.js";
const workspace = document.querySelector("#teamWorkspace");
const status = document.querySelector("#teamStatus");
const join = document.querySelector("#teamJoin");
const signIn = document.querySelector("#teamSignIn");
const library = document.querySelector("#teamLibrary");
const upload = document.querySelector("#teamUpload");
let uploadId = "";
async function task(action, form) {
    const submit = form?.querySelector("button");
    if (submit)
        submit.disabled = true;
    status.textContent = "";
    try {
        await action();
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        if (submit)
            submit.disabled = false;
    }
}
function reveal(member) {
    if (!library || !signIn || !upload)
        return;
    signIn.hidden = Boolean(member);
    library.hidden = !member;
    const nav = library.querySelector("#teamAdminLinks");
    nav.hidden = member?.role !== "admin";
    upload.hidden = !member || member.role === "viewer";
    if (member) {
        if (member.role === "admin")
            sessionStorage.setItem("cueporchOperator", sessionMarker);
        else
            clearOperatorMarker();
        library.querySelector("#teamIdentity").textContent = `${member.name} · ${roleLabels[member.role]}`;
    }
    else
        clearOperatorMarker();
}
function clearOperatorMarker() {
    if (sessionStorage.getItem("cueporchOperator") === sessionMarker)
        sessionStorage.removeItem("cueporchOperator");
}
async function showVideos() {
    const response = await checked(await teamRequest("library"));
    const { works } = await response.json();
    const list = document.querySelector("#teamVideos");
    const empty = document.querySelector("#teamLibraryEmpty");
    list.replaceChildren();
    empty.hidden = works.length > 0;
    for (const work of works) {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = work.watch;
        link.textContent = work.title;
        item.append(link);
        list.append(item);
    }
}
if (join) {
    const invitation = location.hash.slice(1);
    history.replaceState(null, "", location.pathname);
    if (!/^[A-Za-z0-9_-]{43}$/.test(invitation)) {
        join.hidden = true;
        status.textContent = "Open the complete invitation link to join the team.";
    }
    join.onsubmit = event => {
        event.preventDefault();
        void task(async () => {
            await checked(await teamRequest("join", "POST", { ...Object.fromEntries(new FormData(join)), invitation }));
            join.reset();
            location.assign("/team");
        }, join);
    };
}
else if (signIn && upload) {
    signIn.onsubmit = event => {
        event.preventDefault();
        void task(async () => {
            const response = await checked(await teamRequest("session", "POST", Object.fromEntries(new FormData(signIn))));
            signIn.reset();
            reveal(await response.json());
            await showVideos();
        }, signIn);
    };
    document.querySelector("#teamSignOut").onclick = () => void task(async () => {
        await checked(await teamRequest("session", "DELETE"));
        document.querySelector("#teamVideos").replaceChildren();
        reveal();
    });
    const file = upload.querySelector("input[type=file]");
    const progress = upload.querySelector("progress");
    file.onchange = () => { uploadId = crypto.randomUUID(); };
    upload.onsubmit = event => {
        event.preventDefault();
        const video = file.files?.[0];
        if (!video)
            return;
        const id = uploadId || (uploadId = crypto.randomUUID());
        void task(async () => {
            progress.hidden = false;
            file.disabled = true;
            try {
                await uploadLibraryFile(video, id, "", sessionMarker, null, value => {
                    if (value === null)
                        progress.removeAttribute("value");
                    else
                        progress.value = value;
                }, "/api/team/uploads");
                upload.reset();
                uploadId = "";
                await showVideos();
                status.textContent = "Private video added to My Library.";
            }
            finally {
                progress.hidden = true;
                file.disabled = false;
            }
        }, upload);
    };
    void task(async () => {
        const response = await teamRequest("account");
        if (response.status === 401)
            return reveal();
        reveal(await (await checked(response)).json());
        await showVideos();
    });
}
workspace.hidden = false;
