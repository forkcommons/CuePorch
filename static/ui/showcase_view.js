const copy = document.querySelector("#showcaseCopy");
if (copy) {
    copy.onclick = async () => {
        const status = document.querySelector("#showcaseCopyStatus");
        const link = new URL(location.pathname, location.origin).href;
        try {
            await navigator.clipboard.writeText(link);
            status.textContent = "Showcase link copied.";
        }
        catch {
            status.textContent = link;
        }
    };
}
const contents = document.querySelector("#showcaseContents");
if (contents) {
    const items = JSON.parse(contents.textContent);
    const buttons = Array.from(document.querySelectorAll("button[data-index]"));
    const video = document.querySelector(".showcase-screen video");
    const heading = document.querySelector("#nowPlaying");
    let current = 0;
    function playItem(index) {
        const item = items[index];
        if (index !== current) {
            buttons[current].removeAttribute("aria-current");
            buttons[index].setAttribute("aria-current", "true");
            current = index;
            heading.textContent = item.title;
            video.poster = item.poster || "";
            video.replaceChildren(...item.captions.map(({ language, url }) => {
                const track = document.createElement("track");
                Object.assign(track, { kind: "captions", srclang: language, label: language, src: url });
                return track;
            }));
            video.src = item.media;
        }
        else if (video.ended) {
            video.currentTime = 0;
        }
        void video.play().catch(() => { });
    }
    buttons.forEach((button, index) => { button.onclick = () => playItem(index); });
    video.onended = () => {
        if (document.body.dataset.next !== "yes")
            return;
        if (current + 1 < items.length)
            playItem(current + 1);
        else if (document.body.dataset.loop === "yes")
            playItem(0);
    };
    const search = document.querySelector("#showcaseSearch");
    if (search) {
        search.oninput = () => {
            const query = search.value.trim().toLocaleLowerCase();
            let matches = 0;
            buttons.forEach((button, index) => {
                const matchesQuery = items[index].title.toLocaleLowerCase().includes(query);
                button.closest("li").hidden = !matchesQuery;
                if (matchesQuery)
                    matches += 1;
            });
            document.querySelector("#showcaseNoMatches").hidden = matches !== 0;
        };
    }
}
export {};
