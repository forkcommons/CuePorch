import { playbackObservation } from "./playback_analytics.js";
import { setupVideoQuestions } from "./video_questions.js";
import { setupVideoLikes } from "./video_community.js";
import { setupWatchComments } from "./watch_comments.js";
import { createShowcaseRegistration } from "./showcase_member_registration.js";
function copyLink(id, makeLink, confirmation) {
    const button = document.querySelector("#" + id);
    if (!button)
        return;
    button.onclick = async () => {
        const status = document.querySelector("#" + id + "Status");
        const link = makeLink().href;
        try {
            await navigator.clipboard.writeText(link);
            status.textContent = confirmation;
        }
        catch {
            status.textContent = link;
        }
    };
}
const galleryLink = () => new URL(location.pathname, location.origin);
copyLink("showcaseCopy", galleryLink, "Showcase link copied.");
function videoLink(id) {
    const link = galleryLink();
    link.searchParams.set("video", id);
    return link;
}
const contents = document.querySelector("#showcaseContents");
if (contents) {
    const items = JSON.parse(contents.textContent);
    const registration = createShowcaseRegistration();
    const cards = Array.from(document.querySelectorAll(".showcase-playlist [data-index]"));
    const clips = Array.from(document.querySelectorAll(".showcase-playlist video"));
    const video = document.querySelector(".showcase-screen video");
    const heading = document.querySelector("#nowPlaying");
    const description = document.querySelector("#showcasePlayingDescription");
    const download = document.querySelector("#showcaseDownload");
    let current = cards.findIndex((card) => card.hasAttribute("aria-current"));
    const observe = items.map((item) => item.analytics ? playbackObservation(item.analytics, true) : undefined);
    const questions = document.querySelector("#showcaseQuestions");
    const setQuestions = questions ? setupVideoQuestions(questions, () => video || clips[current]) : undefined;
    const community = document.querySelector("#showcaseCommunity");
    const communityTemplate = document.querySelector("#showcaseCommunityTemplate");
    copyLink("showcaseCopyVideo", () => videoLink(items[current].id), "Link to this video copied.");
    function renderCommunity(item) {
        if (!community || !communityTemplate)
            return;
        community.replaceChildren();
        if (!item.likes && !item.comments)
            return;
        const controls = communityTemplate.content.cloneNode(true);
        const link = videoLink(item.id);
        const next = link.pathname + link.search;
        const likes = controls.querySelector("[data-showcase-likes]");
        const comments = controls.querySelector("[data-watch-comments]");
        if (likes && !item.likes)
            likes.remove();
        if (comments && !item.comments)
            comments.remove();
        community.append(controls);
        if (likes?.isConnected) {
            Object.assign(likes.dataset, { slug: item.id, next });
            setupVideoLikes(likes);
        }
        if (comments?.isConnected) {
            Object.assign(comments.dataset, { slug: item.id, next });
            setupWatchComments(comments);
        }
    }
    renderCommunity(items[current]);
    function setCurrent(index) {
        if (index === current)
            return;
        registration.closeForSelection();
        cards[current].removeAttribute("aria-current");
        cards[index].setAttribute("aria-current", "true");
        current = index;
        const item = items[index];
        heading.textContent = item.title;
        description.textContent = item.description;
        description.hidden = !item.description;
        download.hidden = !item.download;
        if (item.download)
            download.href = item.download;
        else
            download.removeAttribute("href");
        setQuestions?.(item.questions);
        renderCommunity(item);
    }
    function advance(playItem) {
        if (document.body.dataset.next !== "yes")
            return;
        if (current + 1 < items.length)
            playItem(current + 1);
        else if (document.body.dataset.loop === "yes")
            playItem(0);
    }
    if (video) {
        let detach = observe[current]?.(video, true);
        function playItem(index, player) {
            const item = items[index];
            if (index !== current) {
                detach?.();
                setCurrent(index);
                player.poster = item.poster || "";
                player.replaceChildren(...item.captions.map(({ language, url, key }) => {
                    const track = document.createElement("track");
                    Object.assign(track, { kind: "captions", srclang: language, label: language, src: url });
                    track.dataset.analyticsTrack = key;
                    return track;
                }));
                player.src = item.media;
                detach = observe[index]?.(player);
            }
            else if (player.ended) {
                player.currentTime = 0;
            }
            void player.play().catch(() => { });
        }
        cards.forEach((card, index) => { card.onclick = () => playItem(index, video); });
        video.onplay = () => { registration.guardPlay(video); };
        video.ontimeupdate = () => registration.onProgress(items[current], video);
        video.onended = () => {
            const advanceCurrent = () => advance((index) => playItem(index, video));
            if (!registration.onEnd(items[current], video, advanceCurrent))
                advanceCurrent();
        };
    }
    else {
        const unsampled = new Map();
        const visible = new IntersectionObserver((entries) => {
            for (const entry of entries) {
                if (entry.isIntersecting)
                    unsampled.get(entry.target)?.();
            }
        });
        clips.forEach((clip, index) => {
            if (observe[index]) {
                unsampled.set(clip, () => {
                    visible.unobserve(clip);
                    unsampled.delete(clip);
                    observe[index]?.(clip, true);
                });
                visible.observe(clip);
            }
            clip.onplay = () => {
                if (registration.guardPlay(clip))
                    return;
                unsampled.get(clip)?.();
                for (const other of clips)
                    if (other !== clip)
                        other.pause();
                setCurrent(index);
                registration.onProgress(items[index], clip);
            };
            clip.ontimeupdate = () => {
                if (current === index)
                    registration.onProgress(items[index], clip);
            };
            clip.onended = () => {
                if (current !== index)
                    return;
                const advanceCurrent = () => advance((next) => {
                    clips[next].muted = clip.muted;
                    clips[next].currentTime = 0;
                    void clips[next].play().catch(() => { });
                });
                if (!registration.onEnd(items[index], clip, advanceCurrent))
                    advanceCurrent();
            };
        });
    }
    const search = document.querySelector("#showcaseSearch");
    if (search) {
        search.oninput = () => {
            const query = search.value.trim().toLocaleLowerCase();
            let matches = 0;
            cards.forEach((card, index) => {
                const matchesQuery = items[index].title.toLocaleLowerCase().includes(query);
                card.closest("li").hidden = !matchesQuery;
                if (matchesQuery)
                    matches += 1;
            });
            document.querySelector("#showcaseNoMatches").hidden = matches !== 0;
        };
    }
}
