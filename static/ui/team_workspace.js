import { editFolderSlack } from "./folder_slack.js";
import { uploadLibraryFile } from "./library_upload.js";
import { checked, roleLabels, sessionMarker, teamRequest } from "./team_api.js";
import { requestSignInCode, resetSignInCode, signInValues } from "./sign_in_factor.js";
const workspace = document.querySelector("#teamWorkspace");
const status = document.querySelector("#teamStatus");
const join = document.querySelector("#teamJoin");
const signIn = document.querySelector("#teamSignIn");
const library = document.querySelector("#teamLibrary");
const upload = document.querySelector("#teamUpload");
const search = document.querySelector("#teamSearch");
const clearSearch = document.querySelector("#clearTeamSearch");
const slackForm = document.querySelector("#teamSlackFolder");
const slackFolder = slackForm?.querySelector("select");
const slackDialog = document.querySelector("#teamSlackDialog");
const slackHeading = document.querySelector("#teamSlackHeading");
const slackBody = document.querySelector("#teamSlackBody");
const slackStatus = document.querySelector("#teamSlackDialogStatus");
let managesFolderSlack = false;
async function openTeamSlack() {
    const option = slackFolder?.selectedOptions[0];
    if (!option?.value || !slackDialog || !slackHeading || !slackBody || !slackStatus)
        return;
    await editFolderSlack({ id: option.value, name: option.text }, {
        request: (path, method = "GET", body, headers) => fetch(path, {
            method, headers: {
                Authorization: `Bearer ${sessionMarker}`,
                ...(body ? { "Content-Type": "application/json" } : {}),
                ...headers,
            },
            body: body ? JSON.stringify(body) : undefined,
        }),
        checked,
        openDialog: title => {
            slackHeading.textContent = title;
            slackStatus.textContent = "";
            slackBody.replaceChildren();
            if (!slackDialog.open)
                slackDialog.showModal();
            return slackBody;
        },
        status: slackStatus,
    });
}
async function showSlackFolders(requestId) {
    if (!managesFolderSlack || !slackForm || !slackFolder)
        return;
    const response = await teamRequest("slack-folders", "GET", {}, sessionMarker);
    if (requestId !== libraryRequest || !managesFolderSlack)
        return;
    if (response.status === 401 || response.status === 403) {
        slackForm.hidden = true;
        slackFolder.replaceChildren();
        slackDialog?.close();
        return;
    }
    const { folders } = await (await checked(response)).json();
    if (requestId !== libraryRequest || !managesFolderSlack)
        return;
    const previous = slackFolder.value;
    slackFolder.replaceChildren(...folders.map(folder => new Option(folder.title, folder.id)));
    if (folders.some(folder => folder.id === previous))
        slackFolder.value = previous;
    slackForm.hidden = folders.length === 0;
    const address = new URL(location.href);
    const result = address.searchParams.get("slack");
    if (result !== "connected" && result !== "cancelled")
        return;
    const folder = address.searchParams.get("folder");
    address.searchParams.delete("slack");
    address.searchParams.delete("folder");
    history.replaceState(null, "", address);
    if (!folders.some(option => option.id === folder))
        return;
    slackFolder.value = folder;
    await openTeamSlack();
    if (slackStatus)
        slackStatus.textContent = result === "connected"
            ? "Slack channel connected." : "Slack channel selection was canceled.";
}
let uploadId = "";
let libraryQuery = "";
let libraryRequest = 0;
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
    if (!member) {
        const activity = library.querySelector(".workspace-activity");
        if (activity)
            activity.open = false;
    }
    signIn.hidden = Boolean(member);
    library.hidden = !member;
    const nav = library.querySelector("#teamAdminLinks");
    const profile = library.querySelector("#editCreatorProfile");
    const managesTeam = member?.role === "admin" || member?.role === "owner";
    nav.hidden = !managesTeam;
    upload.hidden = !member || member.role === "viewer";
    profile.hidden = upload.hidden;
    if (member) {
        if (managesTeam)
            sessionStorage.setItem("cueporchOperator", sessionMarker);
        else
            clearOperatorMarker();
        library.querySelector("#teamIdentity").textContent = `${member.name} · ${roleLabels[member.role]}`;
    }
    else
        clearOperatorMarker();
    managesFolderSlack = member?.role === "contributor" || member?.role === "contributor_plus";
    if (!managesFolderSlack && slackForm) {
        slackForm.hidden = true;
        slackFolder?.replaceChildren();
        slackDialog?.close();
    }
    resetSignInCode(signIn);
}
function clearOperatorMarker() {
    if (sessionStorage.getItem("cueporchOperator") === sessionMarker)
        sessionStorage.removeItem("cueporchOperator");
}
async function showVideos() {
    const requestId = ++libraryRequest;
    const query = libraryQuery;
    const response = await teamRequest(query ? "library?" + new URLSearchParams({ q: query }) : "library");
    if (requestId !== libraryRequest)
        return;
    if (response.status === 401) {
        document.querySelector("#teamVideos").replaceChildren();
        reveal();
    }
    const { works, workspaces } = await (await checked(response)).json();
    if (requestId !== libraryRequest)
        return;
    const linked = document.querySelector("#linkedWorkspaces");
    linked.querySelector("ul").replaceChildren(...workspaces.map(workspace => {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = new URL("/team", workspace.origin).href;
        link.rel = "noreferrer";
        link.textContent = workspace.name;
        item.append(link);
        return item;
    }));
    linked.hidden = workspaces.length === 0;
    const list = document.querySelector("#teamVideos");
    const empty = document.querySelector("#teamLibraryEmpty");
    list.replaceChildren();
    empty.hidden = works.length > 0;
    empty.textContent = query ? "No videos in your Team Library match your search." : "There are no videos available to you yet.";
    if (clearSearch)
        clearSearch.hidden = !query;
    for (const work of works) {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = work.watch;
        link.textContent = work.title;
        item.append(link);
        list.append(item);
    }
    await showSlackFolders(requestId);
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
else if (signIn && upload && search && clearSearch) {
    if (slackForm)
        slackForm.onsubmit = event => {
            event.preventDefault();
            void task(openTeamSlack, slackForm);
        };
    const searchInput = search.querySelector("[name=q]");
    search.onsubmit = event => {
        event.preventDefault();
        const query = searchInput.value.trim();
        if (new FormData(search).get("scope") === "watch") {
            location.assign("/watch?" + new URLSearchParams({ q: query }));
            return;
        }
        libraryQuery = query;
        void task(showVideos, search);
    };
    clearSearch.onclick = () => {
        searchInput.value = libraryQuery = "";
        searchInput.focus();
        void task(showVideos);
    };
    signIn.querySelector("input[type=email]").oninput = () => resetSignInCode(signIn);
    signIn.onsubmit = event => {
        event.preventDefault();
        void task(async () => {
            const response = await teamRequest("session", "POST", signInValues(signIn, event.submitter));
            if (response.status === 428)
                return requestSignInCode(signIn, status, await response.json());
            await checked(response);
            signIn.reset();
            reveal(await response.json());
            await showVideos();
        }, signIn);
    };
    document.querySelector("#teamSignOut").onclick = () => void task(async () => {
        await checked(await teamRequest("session", "DELETE"));
        libraryRequest += 1;
        libraryQuery = "";
        search.reset();
        clearSearch.hidden = true;
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
