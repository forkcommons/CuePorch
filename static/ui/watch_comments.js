import { viewerRequest, viewerVideo } from "./audience_api.js";
for (const section of document.querySelectorAll("[data-watch-comments]")) {
    const video = viewerVideo(section);
    const endpoint = "comments/" + video.path;
    const signIn = section.querySelector("[data-comment-signin]");
    const form = section.querySelector("[data-comment-post]");
    const submit = form.querySelector("button");
    const search = section.querySelector("[data-comment-search]");
    const own = section.querySelector("[data-comment-own]");
    const ownList = section.querySelector("[data-comment-own-list]");
    const list = section.querySelector("[data-comment-list]");
    const more = section.querySelector("[data-comment-more]");
    const status = section.querySelector("[data-comment-status]");
    let query = new URLSearchParams();
    let page = 1;
    let loadVersion = 0;
    function comment(entry) {
        const item = document.createElement("li");
        const byline = document.createElement("p");
        const author = document.createElement("strong");
        const date = document.createElement("time");
        const created = new Date(entry.created * 1000);
        author.textContent = entry.author;
        date.dateTime = created.toISOString();
        date.textContent = created.toLocaleString();
        byline.append(author, " · ", date);
        const body = document.createElement("p");
        body.className = "watch-comment-body";
        body.textContent = entry.body;
        item.append(byline, body);
        if (entry.status !== "eligible") {
            const decision = document.createElement("p");
            decision.textContent = entry.status === "waiting" ? "Awaiting approval" : "Not approved";
            item.append(decision);
        }
        if (entry.owned) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "Delete comment";
            remove.onclick = async () => {
                remove.disabled = true;
                try {
                    const response = await viewerRequest(`${endpoint}/${entry.id}`, "DELETE");
                    if (response.status === 401) {
                        location.assign(video.account);
                        return;
                    }
                    if (!response.ok)
                        throw new Error();
                    await load(1, "Comment deleted.");
                }
                catch {
                    status.textContent = "Could not delete the comment. Try again.";
                }
                finally {
                    remove.disabled = false;
                }
            };
            item.append(remove);
        }
        return item;
    }
    async function load(nextPage = 1, notice = "") {
        const version = ++loadVersion;
        const parameters = new URLSearchParams(query);
        parameters.set("page", String(nextPage));
        section.setAttribute("aria-busy", "true");
        more.disabled = true;
        try {
            const response = await viewerRequest(`${endpoint}?${parameters}`);
            if (version !== loadVersion)
                return;
            if (response.status === 404) {
                section.hidden = true;
                return;
            }
            if (!response.ok)
                throw new Error();
            const data = await response.json();
            if (version !== loadVersion)
                return;
            signIn.hidden = data.signed_in;
            form.hidden = !data.signed_in;
            ownList.replaceChildren(...data.own_unpublished.map(comment));
            own.hidden = ownList.children.length === 0;
            const entries = data.comments.map(comment);
            if (nextPage === 1)
                list.replaceChildren(...entries);
            else
                list.append(...entries);
            page = nextPage;
            more.hidden = !data.more;
            status.textContent = notice || (list.children.length ? "" : "No comments found.");
            section.hidden = false;
        }
        catch {
            if (version === loadVersion) {
                section.hidden = false;
                status.textContent = "Could not load comments. Try again.";
            }
        }
        finally {
            if (version === loadVersion) {
                section.removeAttribute("aria-busy");
                more.disabled = false;
            }
        }
    }
    search.onsubmit = event => {
        event.preventDefault();
        const values = new FormData(search);
        query = new URLSearchParams({ q: String(values.get("q")), sort: String(values.get("sort")) });
        void load();
    };
    more.onclick = () => void load(page + 1);
    form.onsubmit = async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
            const response = await viewerRequest(endpoint, "POST", Object.fromEntries(new FormData(form)));
            if (response.status === 401) {
                location.assign(video.account);
                return;
            }
            if (response.status === 429) {
                status.textContent = "Too many comments. Try again later.";
                return;
            }
            if (!response.ok)
                throw new Error();
            const result = await response.json();
            form.reset();
            await load(1, result.status === "waiting" ? "Comment submitted for approval." : "Comment posted.");
        }
        catch {
            status.textContent = "Could not post the comment. Try again.";
        }
        finally {
            submit.disabled = false;
        }
    };
    void load();
}
