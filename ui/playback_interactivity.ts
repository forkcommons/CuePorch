export {};

const player = document.querySelector<HTMLElement>(".interactive-player")!;
const video = player.querySelector<HTMLVideoElement>("video")!;
const cards = [...player.querySelectorAll<HTMLElement>(".video-timed-card")];
const ending = player.querySelector<HTMLElement>("[data-ending]");
const share = ending?.querySelector<HTMLInputElement>("[data-share]");
const dismissed = new Set<HTMLElement>();
let finished = false;

function updateCards() {
  for (const card of cards) {
    card.hidden = finished || dismissed.has(card)
      || video.currentTime < Number(card.dataset.start) || video.currentTime >= Number(card.dataset.end);
  }
}

for (const card of cards) {
  card.querySelector<HTMLButtonElement>("[data-dismiss]")!.onclick = () => {
    dismissed.add(card);
    card.hidden = true;
  };
}
if (share) {
  share.value = new URL(share.dataset.share!, location.origin).href;
  share.onclick = () => share.select();
  ending!.querySelector<HTMLAnchorElement>("[data-email-share]")!.href = "mailto:?body=" + encodeURIComponent(share.value);
  const copy = ending!.querySelector<HTMLButtonElement>("[data-copy-share]")!;
  copy.onclick = async () => {
    share.select();
    try {
      await navigator.clipboard.writeText(share.value);
      copy.textContent = "Copied";
    } catch {
      share.focus();
    }
  };
}
const replay = ending?.querySelector<HTMLButtonElement>("[data-replay]");
if (replay) replay.onclick = () => {
  video.currentTime = 0;
  void video.play().catch(() => { video.controls = true; });
};
video.addEventListener("timeupdate", updateCards);
video.addEventListener("seeked", updateCards);
video.addEventListener("ended", () => {
  finished = true;
  updateCards();
  if (!ending) return;
  ending.hidden = false;
  if (ending.dataset.ending === "thumbnail") {
    video.currentTime = 0;
    if (video.poster) ending.style.backgroundImage = `url("${video.poster}")`;
  }
});
video.addEventListener("play", () => {
  if (finished) dismissed.clear();
  finished = false;
  if (ending) ending.hidden = true;
  updateCards();
});
