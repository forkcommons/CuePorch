/** Produces accessible iframe markup from the current video's permitted embed URL. */
function attribute(value) {
    return value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
export function embedBuilder() {
    const controls = document.querySelector("#embedOptions");
    const code = document.querySelector("#workspaceEmbed");
    const label = document.querySelector("#workspaceEmbedLabel");
    const copy = document.querySelector("#copyEmbed");
    const scriptLabel = document.querySelector("#embedApiOption");
    const privacyLabel = document.querySelector("#embedPrivacyOption");
    const input = (name) => controls.querySelector(`[name=${name}]`);
    let path;
    let title;
    let native = false;
    function update() {
        if (!path)
            return;
        const url = new URL(path);
        for (const name of ["autoplay", "muted", "loop"]) {
            if (input(name).checked)
                url.searchParams.set(name, "1");
        }
        const useApi = native && input("api").checked;
        if (useApi)
            url.searchParams.set("api", "1");
        if (native && input("dnt").checked)
            url.searchParams.set("dnt", "1");
        const width = input("width");
        const height = input("height");
        if (!width.validity.valid || !height.validity.valid) {
            code.value = "";
            copy.disabled = true;
            return;
        }
        const dimensions = input("responsive").checked
            ? `style="width:100%;aspect-ratio:${width.valueAsNumber}/${height.valueAsNumber};border:0"`
            : `width="${width.valueAsNumber}" height="${height.valueAsNumber}" style="border:0"`;
        code.value = `<iframe src="${attribute(url.href)}" title="${attribute(title)}" ${dimensions} referrerpolicy="origin" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen></iframe>`;
        if (useApi) {
            code.value += `\n<script type="module" src="${attribute(new URL("/player.js", url).href)}"></script>`;
        }
        copy.disabled = false;
    }
    controls.addEventListener("input", update);
    return {
        show(url, playerTitle, ownPlayer) {
            path = url;
            title = playerTitle;
            native = ownPlayer;
            controls.hidden = label.hidden = copy.hidden = !path;
            scriptLabel.hidden = privacyLabel.hidden = !native;
            update();
        },
    };
}
