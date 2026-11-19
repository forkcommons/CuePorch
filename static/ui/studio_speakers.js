import { mediaPreview } from "./recording_composition.js";
import { gather, speakerRequest, SpeakerRequestError } from "./speaker_connection.js";
import { speakerChat } from "./speaker_chat.js";
export function setupSpeakers(path, credential, onChange) {
    const panel = document.querySelector("#studioSpeakers");
    const roster = panel.querySelector("#studioSpeakerList");
    const invite = panel.querySelector("#inviteSpeaker");
    const link = panel.querySelector("#studioSpeakerLink");
    const review = panel.querySelector("#reviewSpeakers");
    const inviteName = invite.elements.namedItem("name");
    const status = panel.querySelector("#studioSpeakerStatus");
    const chat = speakerChat(path, credential);
    const feeds = new Map();
    const connecting = new Map();
    let room = null;
    let refreshBusy = false;
    let editBusy = false;
    let closed = false;
    let lastSeen = 0;
    let rendered = "";
    function api(suffix = "", method = "GET", data) {
        return speakerRequest(path + suffix, credential(), method, data);
    }
    function report(error) { status.textContent = error instanceof Error ? error.message : String(error); }
    function remove(id) {
        const feed = feeds.get(id);
        if (!feed)
            return;
        feeds.delete(id);
        feed.pc.close();
        feed.stream.getTracks().forEach(track => track.stop());
        if (feed.preview)
            feed.preview.srcObject = null;
    }
    function activeFeeds() {
        return (room?.speakers || []).flatMap(speaker => {
            const feed = feeds.get(speaker.id);
            return feed && speaker.on_air && feed.pc.connectionState === "connected" && feed.stream.getTracks().length
                ? [{ id: speaker.id, name: speaker.name, stream: feed.stream, preview: feed.preview }]
                : [];
        });
    }
    function action(label, when, disabled = false) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.disabled = disabled;
        button.onclick = () => void edit(when);
        return button;
    }
    function render() {
        const state = room?.speakers || [];
        const rosterState = JSON.stringify(state.map(speaker => {
            const feed = feeds.get(speaker.id);
            return [speaker, feed?.pc.connectionState, Boolean(feed?.preview)];
        }));
        if (rosterState === rendered)
            return;
        rendered = rosterState;
        const focused = document.activeElement?.closest("[data-speaker]")?.dataset.speaker;
        const focusText = document.activeElement?.textContent;
        roster.replaceChildren(...state.map(speaker => {
            const row = document.createElement("li");
            row.dataset.speaker = speaker.id;
            const feed = feeds.get(speaker.id);
            const connection = feed?.pc.connectionState;
            const ready = connection === "connected";
            const label = document.createElement("p");
            label.textContent = `${speaker.name} · ${!speaker.joined ? "Invited" : !speaker.admitted ? "Waiting for admission" :
                ready ? (speaker.on_air ? "On air" : "Connected · Off air") : !speaker.connected ? "Disconnected" : "Connecting"}`;
            row.append(label);
            if (feed?.preview) {
                feed.preview.width = 192;
                feed.preview.setAttribute("aria-label", `Preview of ${speaker.name}`);
                row.append(feed.preview);
                void feed.preview.play().catch(() => { });
            }
            const endpoint = "/" + speaker.id;
            if (speaker.joined && !speaker.admitted)
                row.append(action("Admit " + speaker.name, () => api(endpoint, "PUT", { admit: true })));
            if (speaker.admitted)
                row.append(action(`${speaker.on_air ? "Take off air" : "Put on air"} · ${speaker.name}`, () => api(endpoint, "PUT", { on_air: !speaker.on_air }), !ready));
            row.append(action("Remove " + speaker.name, () => api(endpoint, "DELETE")));
            return row;
        }));
        if (focused) {
            const item = [...roster.children].find(row => row.dataset.speaker === focused);
            [...(item?.querySelectorAll("button") || [])].find(button => button.textContent === focusText)?.focus();
        }
        onChange();
    }
    async function connect(speaker, revision) {
        const suffix = "/" + speaker.id;
        connecting.set(speaker.id, revision);
        let feed;
        try {
            const offer = await api(suffix);
            if (closed || offer.revision !== revision)
                return;
            const pc = new RTCPeerConnection({ iceServers: offer.ice });
            const stream = new MediaStream();
            feed = { revision, pc, stream, preview: null };
            const into = feed;
            feeds.set(speaker.id, into);
            pc.ontrack = ({ track }) => {
                if (feeds.get(speaker.id) !== into)
                    return;
                into.stream.addTrack(track);
                into.preview ??= mediaPreview(into.stream);
                render();
                onChange();
            };
            pc.onconnectionstatechange = () => {
                if (feeds.get(speaker.id) === into)
                    render();
            };
            await pc.setRemoteDescription({ type: "offer", sdp: offer.offer });
            const answer = await gather(pc, await pc.createAnswer());
            if (feeds.get(speaker.id) === into)
                await api(suffix, "PUT", { revision, answer });
        }
        catch (error) {
            if (feed && feeds.get(speaker.id) === feed)
                remove(speaker.id);
            if (!(error instanceof SpeakerRequestError && [404, 409].includes(error.status)))
                report(error);
        }
        finally {
            if (connecting.get(speaker.id) === revision)
                connecting.delete(speaker.id);
            render();
        }
    }
    async function refresh() {
        if (closed || refreshBusy || !credential())
            return;
        refreshBusy = true;
        try {
            const result = await api();
            if (closed)
                return;
            room = result;
            chat.show(panel.open);
            lastSeen = performance.now();
            if (!editBusy)
                review.checked = result.review;
            for (const [id, feed] of feeds) {
                if (!result.speakers.some(speaker => speaker.id === id && speaker.revision === feed.revision && speaker.connected))
                    remove(id);
            }
            for (const speaker of result.speakers) {
                if (speaker.connected && speaker.needs_answer && speaker.revision && !feeds.has(speaker.id) && !connecting.has(speaker.id)) {
                    void connect(speaker, speaker.revision);
                }
            }
            render();
        }
        catch (error) {
            if (performance.now() - lastSeen > 15000 || (error instanceof SpeakerRequestError && error.status === 404)) {
                room = null;
                chat.show(false);
                for (const id of feeds.keys())
                    remove(id);
                render();
            }
            if (panel.open)
                report(error);
        }
        finally {
            refreshBusy = false;
        }
    }
    async function edit(work) {
        if (editBusy)
            return;
        editBusy = true;
        try {
            await work();
        }
        catch (error) {
            report(error);
        }
        finally {
            editBusy = false;
            await refresh();
        }
    }
    invite.onsubmit = event => {
        event.preventDefault();
        void edit(async () => {
            const issued = await api("", "POST", { name: inviteName.value });
            link.value = new URL(issued.link, document.body.dataset.publicUrl || location.origin).href;
            link.select();
            inviteName.value = "";
            status.textContent = "Invitation ready to share.";
        });
    };
    review.onchange = () => void edit(() => api("", "PUT", { review: review.checked }));
    panel.ontoggle = () => { if (panel.open)
        void refresh();
    else
        chat.show(false); };
    const timer = window.setInterval(() => { if (panel.open || room || feeds.size)
        void refresh(); }, 1500);
    return { activeFeeds, close() { closed = true; chat.close(); window.clearInterval(timer); for (const id of feeds.keys())
            remove(id); } };
}
