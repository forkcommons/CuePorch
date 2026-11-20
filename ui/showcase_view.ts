interface Item {
  id: string;
  title: string;
  description: string;
  media: string;
  download: string | null;
  poster: string | null;
  captions: { language: string; url: string }[];
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

const contents = document.querySelector<HTMLScriptElement>("#showcaseContents");
if (contents) {
  const items: Item[] = JSON.parse(contents.textContent!);
  const cards = Array.from(document.querySelectorAll<HTMLElement>(".showcase-playlist [data-index]"));
  const clips = Array.from(document.querySelectorAll<HTMLVideoElement>(".showcase-playlist video"));
  const video = document.querySelector<HTMLVideoElement>(".showcase-screen video");
  const heading = document.querySelector<HTMLElement>("#nowPlaying")!;
  const description = document.querySelector<HTMLElement>("#showcasePlayingDescription")!;
  const download = document.querySelector<HTMLAnchorElement>("#showcaseDownload")!;
  let current = cards.findIndex((card) => card.hasAttribute("aria-current"));
  copyLink("showcaseCopyVideo", () => {
    const link = galleryLink();
    link.searchParams.set("video", items[current].id);
    return link;
  }, "Link to this video copied.");

  function setCurrent(index: number) {
    cards[current].removeAttribute("aria-current");
    cards[index].setAttribute("aria-current", "true");
    current = index;
    const item = items[index];
    heading.textContent = item.title;
    description.textContent = item.description;
    description.hidden = !item.description;
    download.hidden = !item.download;
    if (item.download) download.href = item.download;
    else download.removeAttribute("href");
  }

  function advance(playItem: (index: number) => void) {
    if (document.body.dataset.next !== "yes") return;
    if (current + 1 < items.length) playItem(current + 1);
    else if (document.body.dataset.loop === "yes") playItem(0);
  }

  if (video) {
    function playItem(index: number, player: HTMLVideoElement) {
      const item = items[index];
      if (index !== current) {
        setCurrent(index);
        player.poster = item.poster || "";
        player.replaceChildren(...item.captions.map(({language, url}) => {
          const track = document.createElement("track");
          Object.assign(track, {kind: "captions", srclang: language, label: language, src: url});
          return track;
        }));
        player.src = item.media;
      } else if (player.ended) {
        player.currentTime = 0;
      }
      void player.play().catch(() => {});
    }
    cards.forEach((card, index) => { card.onclick = () => playItem(index, video); });
    video.onended = () => advance((index) => playItem(index, video));
  } else {
    clips.forEach((clip, index) => {
      clip.onplay = () => {
        for (const other of clips) if (other !== clip) other.pause();
        setCurrent(index);
      };
      clip.onended = () => {
        if (current !== index) return;
        advance((next) => {
          clips[next].muted = clip.muted;
          clips[next].currentTime = 0;
          void clips[next].play().catch(() => {});
        });
      };
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
