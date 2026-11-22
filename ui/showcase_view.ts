import {playbackObservation} from "./playback_analytics.js";
import {setupVideoQuestions} from "./video_questions.js";
import {setupVideoLikes} from "./video_community.js";
import {setupWatchComments} from "./watch_comments.js";
import {createShowcaseRegistration} from "./showcase_member_registration.js";
import type {ShowcaseRegistrationItem} from "./showcase_member_registration.js";

interface Item {
  id: string;
  title: string;
  description: string;
  media: string | null;
  download: string | null;
  analytics: string | null;
  questions: string | null;
  likes: boolean;
  comments: boolean;
  poster: string | null;
  captions: { language: string; url: string; key: string }[];
  registration?: ShowcaseRegistrationItem['registration'];
}

function copyLink(id: string, makeLink: () => URL, confirmation: string) {
  const button = document.querySelector<HTMLButtonElement>("#" + id);
  if (!button) return;
  button.onclick = async () => {
    const status = document.querySelector<HTMLElement>("#" + id + "Status")!;
    const link = makeLink().href;
    try {
      await navigator.clipboard.writeText(link);
      status.textContent = confirmation;
    } catch {
      status.textContent = link;
    }
  };
}

const galleryLink = () => new URL(location.pathname, location.origin);
copyLink("showcaseCopy", galleryLink, "Showcase link copied.");
function videoLink(id: string) {
  const link = galleryLink();
  link.searchParams.set("video", id);
  return link;
}

const contents = document.querySelector<HTMLScriptElement>("#showcaseContents");
if (contents) {
  const items: Item[] = JSON.parse(contents.textContent!);
  const registration = createShowcaseRegistration<Item>();
  const beforeButton = document.querySelector<HTMLButtonElement>("#showcaseBeforeRegistration");
  const cards = Array.from(document.querySelectorAll<HTMLElement>(".showcase-playlist [data-index]"));
  const clips = Array.from(document.querySelectorAll<HTMLVideoElement>(".showcase-playlist video"));
  const video = document.querySelector<HTMLVideoElement>(".showcase-screen video");
  const heading = document.querySelector<HTMLElement>("#nowPlaying")!;
  const description = document.querySelector<HTMLElement>("#showcasePlayingDescription")!;
  const download = document.querySelector<HTMLAnchorElement>("#showcaseDownload")!;
  let current = cards.findIndex((card) => card.hasAttribute("aria-current"));
  const observe = items.map((item) => item.analytics ? playbackObservation(item.analytics, true) : undefined);
  const questions = document.querySelector<HTMLDetailsElement>("#showcaseQuestions");
  const setQuestions = questions ? setupVideoQuestions(questions, () => video || clips[current]) : undefined;
  const community = document.querySelector<HTMLElement>("#showcaseCommunity");
  const communityTemplate = document.querySelector<HTMLTemplateElement>("#showcaseCommunityTemplate");
  copyLink("showcaseCopyVideo", () => videoLink(items[current].id), "Link to this video copied.");

  function renderCommunity(item: Item) {
    if (!community || !communityTemplate) return;
    community.replaceChildren();
    if (!item.likes && !item.comments) return;
    const controls = communityTemplate.content.cloneNode(true) as DocumentFragment;
    const link = videoLink(item.id);
    const next = link.pathname + link.search;
    const likes = controls.querySelector<HTMLElement>("[data-showcase-likes]");
    const comments = controls.querySelector<HTMLElement>("[data-watch-comments]");
    if (likes && !item.likes) likes.remove();
    if (comments && !item.comments) comments.remove();
    community.append(controls);
    if (likes?.isConnected) {
      Object.assign(likes.dataset, {slug: item.id, next});
      setupVideoLikes(likes);
    }
    if (comments?.isConnected) {
      Object.assign(comments.dataset, {slug: item.id, next});
      setupWatchComments(comments);
    }
  }
  renderCommunity(items[current]);

  function setCurrent(index: number) {
    if (index === current) return;
    registration.closeForSelection();
    cards[current].removeAttribute("aria-current");
    cards[index].setAttribute("aria-current", "true");
    current = index;
    renderSelection();
  }

  function renderSelection() {
    const item = items[current];
    heading.textContent = item.title;
    description.textContent = item.description;
    description.hidden = !item.description;
    download.hidden = !item.download;
    if (item.download) download.href = item.download;
    else download.removeAttribute("href");
    if (beforeButton) beforeButton.hidden = !!item.media;
    setQuestions?.(item.questions);
    renderCommunity(item);
  }

  function advance(playItem: (index: number) => void) {
    if (document.body.dataset.next !== "yes") return;
    if (current + 1 < items.length) playItem(current + 1);
    else if (document.body.dataset.loop === "yes") playItem(0);
  }

  function applyMedia(player: HTMLVideoElement, item: Item) {
    player.hidden = !item.media;
    player.poster = item.poster || "";
    player.replaceChildren(...item.captions.map(({language, url, key}) => {
      const track = document.createElement("track");
      Object.assign(track, {kind: "captions", srclang: language, label: language, src: url});
      track.dataset.analyticsTrack = key;
      return track;
    }));
    if (item.media) player.src = item.media;
    else {
      player.removeAttribute("src");
      player.load();
    }
  }

  if (video) {
    let detach = observe[current]?.(video, true);
    function playItem(index: number, player: HTMLVideoElement) {
      const item = items[index];
      if (index !== current) {
        detach?.();
        setCurrent(index);
        applyMedia(player, item);
        detach = observe[index]?.(player);
      }
      if (item.media === null) {
        registration.before(item, player, beforeButton, (released) => {
          items[index] = released;
          observe[index] = released.analytics ? playbackObservation(released.analytics, true) : undefined;
          renderSelection();
          applyMedia(player, released);
          detach = observe[index]?.(player);
          player.focus();
          void player.play().catch(() => {});
        });
        return;
      }
      if (player.ended) player.currentTime = 0;
      void player.play().catch(() => {});
    }
    cards.forEach((card, index) => { card.onclick = () => playItem(index, video); });
    if (beforeButton) beforeButton.onclick = () => playItem(current, video);
    video.onplay = () => { registration.guardPlay(video); };
    video.ontimeupdate = () => registration.onProgress(items[current], video);
    video.onended = () => {
      const advanceCurrent = () => advance((index) => playItem(index, video));
      if (!registration.onEnd(items[current], video, advanceCurrent)) advanceCurrent();
    };
  } else {
    const beforeButtons = clips.map(
      (clip) => clip.parentElement?.querySelector<HTMLButtonElement>("[data-showcase-before]") ?? null,
    );

    const playGrid = (index: number): void => {
      const clip = clips[index];
      const item = items[index];
      if (item.media) {
        clip.currentTime = 0;
        void clip.play().catch(() => {});
        return;
      }
      setCurrent(index);
      for (const other of clips) if (other !== clip) other.pause();
      const toggle = beforeButtons[index];
      if (!toggle) return;
      registration.before(item, clip, toggle, (released) => {
        items[index] = released;
        renderSelection();
        applyMedia(clip, released);
        if (released.analytics) playbackObservation(released.analytics, true)(clip, true);
        toggle.hidden = true;
        clip.focus();
        void clip.play().catch(() => {});
      });
    };

    const unsampled = new Map<Element, () => void>();
    const visible = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) unsampled.get(entry.target)?.();
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
        if (registration.guardPlay(clip)) return;
        unsampled.get(clip)?.();
        for (const other of clips) if (other !== clip) other.pause();
        setCurrent(index);
        registration.onProgress(items[index], clip);
      };
      clip.ontimeupdate = () => {
        if (current === index) registration.onProgress(items[index], clip);
      };
      clip.onended = () => {
        if (current !== index) return;
        const advanceCurrent = () => advance((next) => {
          clips[next].muted = clip.muted;
          playGrid(next);
        });
        if (!registration.onEnd(items[index], clip, advanceCurrent)) advanceCurrent();
      };
      beforeButtons[index]?.addEventListener("click", () => playGrid(index));
    });
  }

  const search = document.querySelector<HTMLInputElement>("#showcaseSearch");
  if (search) {
    search.oninput = () => {
      const query = search.value.trim().toLocaleLowerCase();
      let matches = 0;
      cards.forEach((card, index) => {
        const matchesQuery = items[index].title.toLocaleLowerCase().includes(query);
        card.closest("li")!.hidden = !matchesQuery;
        if (matchesQuery) matches += 1;
      });
      document.querySelector<HTMLElement>("#showcaseNoMatches")!.hidden = matches !== 0;
    };
  }
}
