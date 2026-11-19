import { renderCommentText } from "./review_comment_text.js";
const element = (id) => document.getElementById(id);
const field = (form, key) => form.elements.namedItem(key);
function control(label, perform) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.onclick = perform;
    return button;
}
function text(parent, value, tag = "p") {
    const node = document.createElement(tag);
    renderCommentText(node, value);
    parent.append(node);
}
function showList(list, data, build, context = "") {
    const revision = context + JSON.stringify(data);
    if (list.dataset.revision === revision)
        return;
    list.dataset.revision = revision;
    list.replaceChildren(...data.map(build));
}
function questionItem(question) {
    const item = document.createElement("li");
    text(item, question.question, "strong");
    const detail = [question.name || "Anonymous", `${question.votes} votes`];
    if (question.highlighted)
        detail.push("Pinned");
    if (question.state === "pending")
        detail.push("Awaiting approval");
    if (question.state === "answered")
        detail.push("Answered");
    if (question.archived)
        detail.push("Archived");
    text(item, detail.join(" · "));
    if (question.answer)
        text(item, question.answer);
    return item;
}
function pollResults(list, poll) {
    const total = poll.counts?.reduce((left, right) => left + right, 0) || 0;
    for (const [index, option] of poll.choices.entries()) {
        const count = poll.counts?.[index] || 0;
        const percent = total ? Math.round(100 * count / total) : 0;
        text(list, `${option} · ${count} votes (${percent}%)${poll.correct === index ? " · Correct answer" : ""}`, "li");
    }
}
export function setupProducerEngagement(endpoint, fetchState, exportData, projection) {
    const panel = element("operatorEngagement");
    const qa = element("eventQASettings");
    const pollEditor = element("eventPollEditor");
    const answering = element("eventAnswerQuestion");
    const notice = element("operatorEngagementStatus");
    let draft = null;
    let answerTo = null;
    let pending;
    let current;
    function editPoll(poll) {
        pollEditor.reset();
        draft = poll?.id || null;
        field(pollEditor, "question").value = poll?.question || "";
        for (let i = 0; i < 4; i++)
            field(pollEditor, "option" + (i + 1)).value = poll?.choices[i] || "";
        field(pollEditor, "correct").value = poll?.correct == null ? "" : String(poll.correct);
    }
    function clearAnswer() {
        answerTo = null;
        answering.reset();
        answering.hidden = true;
    }
    function render(state, settings) {
        if (settings) {
            for (const key of ["enabled", "anonymous", "review"]) {
                field(qa, key).checked = Boolean(state.qa.policy[key]);
            }
        }
        const stageRevision = projection?.revision() || "";
        function graphic(item, kind, id) {
            if (!projection)
                return;
            const name = kind === "qa" ? "question" : "poll";
            const button = control(projection.shown(kind, id) ? "Hide " + name + " from scene" : "Show " + name + " on scene", () => {
                projection.toggle(kind, id);
                render(state, false);
                panel.querySelector(`[data-event-graphic="${kind}-${id}"]`)?.focus();
            });
            button.dataset.eventGraphic = kind + "-" + id;
            item.append(button);
        }
        showList(element("operatorQuestions"), state.qa.questions, (question) => {
            const item = questionItem(question);
            const command = (type) => void send("POST", { feature: "qa", type, id: question.id });
            if (question.archived)
                item.append(control("Restore question", () => command("restore")));
            else {
                if (question.state === "pending")
                    item.append(control("Publish question", () => command("publish")));
                else {
                    item.append(control(question.highlighted ? "Unpin question" : "Pin question", () => command("highlight")), control("Reply", () => {
                        answerTo = question.id;
                        element("answeringQuestion").textContent = question.question;
                        field(answering, "answer").value = question.answer || "";
                        answering.hidden = false;
                        field(answering, "answer").focus();
                    }));
                    if (state.qa.policy.enabled)
                        graphic(item, "qa", question.id);
                }
                item.append(control("Archive question", () => command("archive")));
            }
            return item;
        }, stageRevision + state.qa.policy.enabled);
        showList(element("operatorPolls"), state.polls, (poll) => {
            const item = document.createElement("li");
            text(item, poll.question, "strong");
            text(item, poll.state);
            const results = document.createElement("ul");
            pollResults(results, poll);
            item.append(results);
            const command = (type) => void send("POST", { feature: "poll", type, id: poll.id });
            if (poll.state === "draft")
                item.append(control("Edit draft", () => editPoll(poll)), control("Open poll", () => command("open")));
            if (poll.state === "open")
                item.append(control("Publish results", () => command("results")));
            if (poll.state === "results")
                item.append(control("Archive poll", () => command("archive")));
            if (poll.state === "open" || poll.state === "results")
                graphic(item, "poll", poll.id);
            return item;
        }, stageRevision);
    }
    const needsState = () => panel.open || projection?.watching();
    async function read(path, method, body, settings = false) {
        if (!needsState() || endpoint() !== path)
            return;
        try {
            const state = await fetchState(path, method, body);
            if (!needsState() || endpoint() !== path)
                return;
            current = state;
            projection?.sync(state);
            if (method !== "GET")
                notice.textContent = "Engagement updated.";
            if (panel.open)
                render(state, settings);
        }
        catch (error) {
            if (method === "GET" && projection) {
                current = undefined;
                projection.sync();
                element("operatorQuestions").replaceChildren();
                element("operatorPolls").replaceChildren();
                delete element("operatorQuestions").dataset.revision;
                delete element("operatorPolls").dataset.revision;
            }
            notice.textContent = error instanceof Error ? error.message : String(error);
        }
    }
    async function send(method = "GET", body, settings = false) {
        if (!needsState() || (pending && method === "GET" && !settings))
            return;
        const path = endpoint();
        const previous = pending;
        const job = previous ? previous.then(() => read(path, method, body, settings)) : read(path, method, body, settings);
        pending = job;
        try {
            await job;
        }
        finally {
            if (pending === job)
                pending = undefined;
        }
    }
    panel.ontoggle = async () => {
        if (!panel.open)
            return;
        qa.inert = true;
        try {
            await send("GET", undefined, true);
        }
        finally {
            qa.inert = false;
        }
    };
    window.setInterval(() => { if (needsState() && (!document.hidden || projection?.watching()))
        void send(); }, 3000);
    qa.onsubmit = (event) => {
        event.preventDefault();
        void send("PUT", { feature: "qa", ...Object.fromEntries(["enabled", "anonymous", "review"].map((key) => [key, field(qa, key).checked])) }, true);
    };
    answering.onsubmit = (event) => {
        event.preventDefault();
        void send("POST", { feature: "qa", type: "answer", id: answerTo, answer: field(answering, "answer").value });
        clearAnswer();
    };
    pollEditor.onsubmit = (event) => {
        event.preventDefault();
        const offered = ["option1", "option2", "option3", "option4"].map((key, index) => ({ index, value: field(pollEditor, key).value.trim() }))
            .filter((option) => option.value);
        const correct = field(pollEditor, "correct").value;
        void send("POST", { feature: "poll", type: "draft", id: draft, question: field(pollEditor, "question").value,
            choices: offered.map((option) => option.value), correct: correct === "" ? null : offered.findIndex((option) => option.index === Number(correct)) });
    };
    element("closeAnswerQuestion").onclick = clearAnswer;
    element("resetEventPoll").onclick = () => editPoll();
    element("exportEventQuestions").onclick = () => void exportData("qa", "cueporch-event-questions.csv");
    element("exportEventPolls").onclick = () => void exportData("poll", "cueporch-event-polls.csv");
    return {
        sceneChanged() { if (current && panel.open)
            render(current, false); },
        close() {
            panel.open = false;
            current = undefined;
            projection?.sync();
            notice.textContent = "";
            clearAnswer();
            editPoll();
            for (const id of ["operatorQuestions", "operatorPolls"]) {
                element(id).replaceChildren();
                delete element(id).dataset.revision;
            }
        },
    };
}
const audience = document.querySelector("#viewerEngagement");
if (audience) {
    const panel = audience;
    const path = panel.dataset.endpoint;
    const viewer = element("eventLobby").dataset.viewerGrant || "";
    const questionForm = element("askQuestion");
    const name = field(questionForm, "name");
    const anonymous = field(questionForm, "anonymous");
    const order = element("questionOrder");
    let session = "";
    let current;
    let pending;
    try {
        session = sessionStorage.getItem(path) || "";
    }
    catch { /* The embed can run without storage. */ }
    function nameField() {
        element("qaViewerName").hidden = anonymous.checked;
        name.required = !anonymous.checked;
    }
    function render(state) {
        current = state;
        const [poll] = state.polls;
        const qa = state.qa;
        panel.hidden = !poll && !qa.policy.enabled;
        element("viewerQA").hidden = !qa.policy.enabled;
        questionForm.hidden = !state.active;
        element("qaViewerAnonymous").hidden = !qa.policy.anonymous;
        if (!qa.policy.anonymous)
            anonymous.checked = false;
        nameField();
        const list = [...qa.questions].sort((a, b) => Number(b.highlighted) - Number(a.highlighted) ||
            (order.value === "popular" ? b.votes - a.votes : 0) || b.sent - a.sent);
        showList(element("viewerQuestions"), list, (question) => {
            const item = questionItem(question);
            if (state.active && question.state !== "pending")
                item.append(control(question.voted ? "Remove vote" : "Upvote question", () => void send({ feature: "qa", type: "vote", id: question.id, enabled: !question.voted })));
            return item;
        }, String(state.active));
        element("viewerPoll").hidden = !poll;
        if (poll) {
            element("audiencePollQuestion").textContent = poll.question;
            const choices = element("audiencePollOptions");
            const revision = String(state.active) + JSON.stringify(poll);
            if (choices.dataset.revision !== revision) {
                choices.replaceChildren();
                choices.dataset.revision = revision;
                if (poll.state === "results")
                    pollResults(choices, poll);
                else
                    for (const [index, label] of poll.choices.entries()) {
                        const item = document.createElement("li");
                        const button = control(label + (poll.choice === index ? " (your vote)" : ""), () => void send({ feature: "poll", id: poll.id, choice: index }));
                        button.disabled = !state.active || poll.choice !== null;
                        item.append(button);
                        choices.append(item);
                    }
            }
        }
    }
    async function fetchAudience(body) {
        try {
            const headers = { "X-CuePorch-Chat-Embed": panel.dataset.embed || "no" };
            if (viewer)
                headers["X-CuePorch-Event-Viewer"] = viewer;
            if (session)
                headers["X-CuePorch-Engagement-Session"] = session;
            if (body)
                headers["Content-Type"] = "application/json";
            const response = await fetch(path, { method: body ? "POST" : "GET", headers,
                body: body ? JSON.stringify(body) : undefined });
            if (response.status === 404) {
                panel.hidden = true;
                return false;
            }
            if (!response.ok) {
                const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
                throw new Error(detail || "Engagement is unavailable.");
            }
            const state = await response.json();
            session = state.session;
            try {
                sessionStorage.setItem(path, session);
            }
            catch { /* Continue using the current in-page session. */ }
            if (body)
                element("engagementStatus").textContent = "";
            render(state);
            return true;
        }
        catch (error) {
            if (body)
                element("engagementStatus").textContent = error instanceof Error ? error.message : String(error);
            return false;
        }
    }
    async function send(body) {
        const previous = pending;
        if (previous && !body)
            return;
        const job = previous ? previous.then(() => fetchAudience(body)) : fetchAudience(body);
        pending = job;
        try {
            return await job;
        }
        finally {
            if (pending === job)
                pending = undefined;
        }
    }
    order.onchange = () => { if (current)
        render(current); };
    anonymous.onchange = nameField;
    questionForm.onsubmit = async (event) => {
        event.preventDefault();
        const question = field(questionForm, "question");
        if (await send({ feature: "qa", type: "question", question: question.value, name: name.value,
            anonymous: anonymous.checked }))
            question.value = "";
    };
    void send();
    window.setInterval(() => { if (!document.hidden)
        void send(); }, 3000);
}
