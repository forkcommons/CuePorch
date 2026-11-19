export function setupPresentation(call, addPicture, timestamp, displayTime, preview) {
    const form = document.querySelector("#presentationEditor");
    const status = document.querySelector("#presentationStatus");
    const rows = document.querySelector("#cardRows");
    const add = document.querySelector("#addTimedCard");
    const save = document.querySelector("#savePresentation");
    const end = document.querySelector("#ending");
    const videos = document.querySelector("#endVideos");
    const template = document.querySelector("#cardRow");
    const mode = end.querySelector("[name=mode]");
    const field = (node, name) => node.querySelector(`[name="${name}"]`);
    let current;
    function manageImage(node, digest) {
        const image = node.querySelector("[name=image]");
        const caption = node.querySelector("[data-image-state]");
        image.dataset.digest = digest || "";
        caption.textContent = digest ? "Image saved" : "";
        node.querySelector("[data-remove-image]").onclick = () => {
            image.value = "";
            image.dataset.digest = "";
            caption.textContent = "";
        };
    }
    async function savedImage(node) {
        const input = node.querySelector("[name=image]");
        const file = input.files?.[0];
        if (file) {
            input.dataset.digest = await addPicture(file);
            input.value = "";
            node.querySelector("[data-image-state]").textContent = "Image saved";
        }
        return input.dataset.digest || null;
    }
    function appendCard(card) {
        const row = template.content.firstElementChild.cloneNode(true);
        field(row, "start").value = displayTime(card.start);
        field(row, "title").value = card.title;
        field(row, "text").value = card.text;
        field(row, "link").value = card.link;
        manageImage(row, card.image);
        row.querySelector("[data-remove-card]").onclick = () => {
            row.remove();
            add.disabled = false;
        };
        rows.append(row);
        add.disabled = rows.children.length >= 100;
        return row;
    }
    function visibleEndControls() {
        for (const item of end.querySelectorAll("[data-mode]")) {
            item.hidden = !item.dataset.mode.split(" ").includes(mode.value);
            for (const input of item.querySelectorAll("input,textarea")) {
                input.disabled = item.hidden;
            }
        }
    }
    function render(value) {
        current = value;
        rows.replaceChildren();
        add.disabled = false;
        current.settings.cards.forEach(appendCard);
        const ending = current.settings.ending;
        mode.value = ending.mode;
        for (const key of ["title", "text", "label", "link"])
            field(end, key).value = ending[key] || "";
        manageImage(end, ending.image);
        videos.replaceChildren();
        for (const video of value.public_videos) {
            const label = document.createElement("label");
            const input = document.createElement("input");
            input.type = "checkbox";
            input.name = "videos";
            input.value = video.id;
            input.checked = ending.videos?.includes(video.id) || false;
            label.append(input, " " + video.title);
            videos.append(label);
        }
        visibleEndControls();
    }
    mode.onchange = visibleEndControls;
    add.onclick = () => {
        const row = appendCard({ start: Math.floor(preview.currentTime || 0), title: "", text: "", link: "", image: null });
        field(row, "title").focus();
    };
    form.onsubmit = async (event) => {
        event.preventDefault();
        save.disabled = true;
        status.textContent = "";
        try {
            const cards = [];
            for (const row of rows.children) {
                cards.push({ start: timestamp(field(row, "start").value), title: field(row, "title").value,
                    text: field(row, "text").value, link: field(row, "link").value, image: await savedImage(row) });
            }
            const ending = { mode: mode.value };
            for (const key of ["title", "text", "label", "link"])
                ending[key] = field(end, key).value;
            if (!end.querySelector("[name=image]").disabled)
                ending.image = await savedImage(end);
            if (ending.mode === "more") {
                ending.videos = Array.from(videos.querySelectorAll(":checked"), (input) => input.value);
            }
            render(await call("/presentation", "PUT", {
                version: current.version, previous: current.settings, cards, ending,
            }));
            status.textContent = "Cards and end screen saved.";
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            save.disabled = false;
        }
    };
    return async () => {
        form.hidden = true;
        try {
            render(await call("/presentation"));
            form.hidden = false;
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : String(error);
            form.hidden = false;
            save.disabled = true;
            add.disabled = true;
        }
    };
}
