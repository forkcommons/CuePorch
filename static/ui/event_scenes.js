import { drawRecording } from "./recording_composition.js";
const element = (id) => document.getElementById(id);
const button = (id) => element(id);
const field = (form, key) => form.elements.namedItem(key);
function placeImage(context, image, x, y, w, h) {
    const scale = Math.min(w / image.naturalWidth, h / image.naturalHeight);
    const width = image.naturalWidth * scale, height = image.naturalHeight * scale;
    context.drawImage(image, x + (w - width) / 2, y + (h - height) / 2, width, height);
}
function paint(context, frame, camera, screen, lowerThird) {
    const { scene, brand, image, logo, video } = frame;
    const { width, height } = context.canvas;
    const isTitle = scene.layout === "title";
    const layout = scene.layout === "title" ? "screen" : scene.layout;
    drawRecording(context, { layout, color: scene.color, background: scene.background }, isTitle ? null : camera, video || (isTitle ? null : screen));
    context.save();
    context.fillStyle = brand.text;
    context.textBaseline = "middle";
    if (!video && image && isTitle)
        placeImage(context, image, 0, 0, width, height);
    else if (!video && isTitle) {
        context.textAlign = "center";
        context.font = `600 ${Math.max(14, height * .09)}px system-ui`;
        context.fillText(scene.title, width / 2, height * .44, width * .90);
        context.font = `${Math.max(10, height * .045)}px system-ui`;
        context.fillText(scene.subtitle, width / 2, height * .56, width * .84);
    }
    if (logo)
        placeImage(context, logo, width * .85, height * .82, width * .12, height * .14);
    if (lowerThird && scene.headline) {
        context.textAlign = "left";
        context.fillStyle = brand.primary;
        context.fillRect(width * .035, height * .78, width * .69, height * .155);
        context.fillStyle = brand.text;
        context.font = `600 ${Math.max(11, height * .045)}px system-ui`;
        context.fillText(scene.headline, width * .061, height * .823, width * .63);
        context.font = `${Math.max(9, height * .031)}px system-ui`;
        context.fillText(scene.credit, width * .061, height * .885, width * .63);
    }
    context.restore();
}
export function setupSceneDeck(path, credential, changed, useAudio) {
    const panel = element("studioScenes");
    const form = element("studioSceneForm");
    const brandForm = element("studioBrandForm");
    const selectedTemplate = element("studioTemplate");
    const report = element("studioSceneStatus");
    const preview = element("studioPreview").getContext("2d", { alpha: false });
    const previews = new Map();
    let deck;
    let selected;
    let current;
    let lowerThird = false;
    let working = false;
    async function request(body) {
        const headers = { Authorization: "Bearer " + credential() };
        if (body)
            headers["Content-Type"] = "application/json";
        const response = await fetch(path, { method: body ? "PUT" : "GET", headers,
            body: body ? JSON.stringify(body) : undefined });
        if (!response.ok) {
            const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
            throw new Error(detail || "Studio scenes are unavailable.");
        }
        return await response.json();
    }
    function readForm() {
        const all = Object.fromEntries(new FormData(form));
        const clip = deck?.videos.find((video) => JSON.stringify([video.work, video.cut]) === all.clip);
        return { ...all, id: selected, image: all.image || null, clip: clip ? [clip.work, clip.cut] : null };
    }
    function showDeck(value) {
        deck = value;
        button("addStudioScene").disabled = false;
        button("saveStudioBrand").disabled = false;
        selectedTemplate.replaceChildren(...value.templates.map((item, index) => new Option(item.title, String(index))));
        for (const [target, key] of [[form, "image"], [brandForm, "logo"]]) {
            field(target, key).replaceChildren(new Option("None", ""), ...value.images.map(item => new Option(item.title, item.id)));
        }
        field(form, "clip").replaceChildren(new Option("None", ""), ...value.videos.map((video) => new Option(video.title, JSON.stringify([video.work, video.cut]))));
        for (const [key, setting] of Object.entries(value.brand))
            field(brandForm, key).value = setting || "";
        const list = element("studioSceneList");
        list.replaceChildren();
        for (const [index, scene] of value.scenes.entries()) {
            const item = document.createElement("li");
            const choose = document.createElement("button");
            choose.type = "button";
            choose.textContent = scene.title;
            choose.setAttribute("aria-pressed", String(selected === scene.id));
            choose.onclick = () => { selected = scene.id; showDeck(value); };
            item.append(choose);
            for (const [label, delta] of [["Move scene up", -1], ["Move scene down", 1]]) {
                const move = document.createElement("button");
                move.type = "button";
                move.textContent = label;
                move.disabled = index + delta < 0 || index + delta >= value.scenes.length;
                move.setAttribute("aria-label", `${label} (${scene.title})`);
                move.onclick = () => run(async () => {
                    const next = [...value.scenes];
                    next.splice(index, 1);
                    next.splice(index + delta, 0, scene);
                    await save(next);
                });
                item.append(move);
            }
            list.append(item);
        }
        const editing = value.scenes.find(scene => scene.id === selected);
        form.hidden = !editing;
        if (editing) {
            for (const [key, setting] of Object.entries(editing)) {
                if (key === "clip")
                    field(form, key).value = setting ? JSON.stringify(setting) : "";
                else if (key !== "id")
                    field(form, key).value = typeof setting === "string" ? setting : "";
            }
            void asset(editing.image).catch(() => { });
        }
        void asset(value.brand.logo).catch(() => { });
    }
    async function asset(uid) {
        if (!uid)
            return undefined;
        const current = previews.get(uid);
        if (current)
            return current.image;
        const response = await fetch(`${path}/image/${encodeURIComponent(uid)}`, { headers: { Authorization: "Bearer " + credential() } });
        if (!response.ok)
            throw new Error("A selected library image is unavailable.");
        const url = URL.createObjectURL(await response.blob());
        const image = new Image();
        image.src = url;
        try {
            await image.decode();
        }
        catch {
            URL.revokeObjectURL(url);
            throw new Error("A selected graphic could not be decoded.");
        }
        if (previews.has(uid))
            URL.revokeObjectURL(url);
        else
            previews.set(uid, { image, url });
        return previews.get(uid).image;
    }
    async function loadClip(clip) {
        if (!clip)
            return undefined;
        const [work, cut] = clip;
        const response = await fetch(`/api/library/work/${encodeURIComponent(work)}/timeline/preview`, {
            method: "POST", headers: { Authorization: "Bearer " + credential(), "Content-Type": "application/json" },
            body: JSON.stringify({ cut }),
        });
        if (!response.ok)
            throw new Error("The retained video is unavailable.");
        const { preview } = await response.json();
        const video = document.createElement("video");
        video.preload = "auto";
        video.playsInline = true;
        video.muted = true;
        await new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => reject(new Error("The scene video could not be loaded.")), 12000);
            video.onloadeddata = () => { window.clearTimeout(timer); resolve(); };
            video.onerror = () => { window.clearTimeout(timer); reject(new Error("The scene video could not be decoded.")); };
            video.src = preview;
        });
        return video;
    }
    function releaseClip() {
        if (current?.video) {
            current.video.pause();
            current.video.removeAttribute("src");
            current.video.load();
            useAudio();
        }
    }
    async function save(scenes, brand = deck.brand) {
        showDeck(await request({ revision: deck.revision, scenes, brand }));
        report.textContent = "Scenes saved.";
    }
    function run(task) {
        if (working)
            return;
        working = true;
        panel.inert = true;
        void task().catch((error) => { report.textContent = error instanceof Error ? error.message : String(error); })
            .finally(() => { panel.inert = false; working = false; });
    }
    function controls() {
        button("hideStudioGraphic").disabled = !current?.scene.headline;
        button("hideStudioGraphic").textContent = lowerThird ? "Hide lower third" : "Show lower third";
        button("clearStudioScene").disabled = !current;
        button("toggleStudioClip").hidden = button("restartStudioClip").hidden = !current?.video;
        button("toggleStudioClip").textContent = current?.video?.paused ? "Play clip" : "Pause clip";
        changed();
    }
    form.onsubmit = (event) => {
        event.preventDefault();
        run(async () => {
            const updated = readForm();
            await save(deck.scenes.map(scene => scene.id === selected ? updated : scene));
        });
    };
    brandForm.onsubmit = (event) => {
        event.preventDefault();
        run(async () => {
            const brand = { primary: field(brandForm, "primary").value, text: field(brandForm, "text").value,
                logo: field(brandForm, "logo").value || null };
            await save(deck.scenes, brand);
        });
    };
    button("addStudioScene").onclick = () => run(async () => {
        const template = deck.templates[Number(selectedTemplate.value)];
        if (!template)
            return;
        selected = crypto.randomUUID().replaceAll("-", "");
        const scene = { ...template, id: selected, headline: "", credit: "", image: null, clip: null };
        await save([...deck.scenes, scene]);
    });
    button("duplicateStudioScene").onclick = () => run(async () => {
        const prior = deck.scenes.find(scene => scene.id === selected);
        if (!prior)
            return;
        selected = crypto.randomUUID().replaceAll("-", "");
        await save([...deck.scenes, { ...prior, id: selected, title: prior.title.slice(0, 65) + " copy" }]);
    });
    button("deleteStudioScene").onclick = () => run(async () => {
        const remaining = deck.scenes.filter(scene => scene.id !== selected);
        selected = remaining.at(-1)?.id;
        await save(remaining);
    });
    button("takeStudioScene").onclick = () => run(async () => {
        const scene = deck?.scenes.find(scene => scene.id === selected);
        if (!scene || !deck)
            return;
        const brand = deck.brand;
        const [image, logo, video] = await Promise.all([asset(scene.image), asset(brand.logo), loadClip(scene.clip)]);
        releaseClip();
        current = { scene, brand, image, logo, video };
        if (video) {
            useAudio(video);
            video.onended = controls;
            await video.play();
        }
        lowerThird = false;
        controls();
        report.textContent = `On stage: ${scene.title}`;
    });
    button("hideStudioGraphic").onclick = () => { lowerThird = !lowerThird; controls(); };
    button("clearStudioScene").onclick = () => {
        releaseClip();
        current = undefined;
        lowerThird = false;
        controls();
        report.textContent = "Camera and screen on stage.";
    };
    button("toggleStudioClip").onclick = () => run(async () => {
        const video = current?.video;
        if (!video)
            return;
        if (video.paused)
            await video.play();
        else
            video.pause();
        controls();
    });
    button("restartStudioClip").onclick = () => run(async () => {
        const video = current?.video;
        if (!video)
            return;
        video.currentTime = 0;
        await video.play();
        controls();
    });
    field(form, "image").onchange = () => void asset(field(form, "image").value || null).catch(() => { });
    return {
        async load() { showDeck(await request()); },
        ready() { return Boolean(current?.video || current?.scene.layout === "title"); },
        stage() { return { id: current?.scene.id ?? "", brand: current?.brand ?? deck?.brand }; },
        draw(context, camera, screen, engagement = false) {
            if (deck && panel.open && !form.hidden) {
                const scene = readForm();
                paint(preview, { scene, brand: deck.brand, image: previews.get(scene.image || "")?.image,
                    logo: previews.get(deck.brand.logo || "")?.image }, camera, screen, true);
            }
            if (current)
                paint(context, current, camera, screen, lowerThird && !engagement);
            return Boolean(current);
        },
        close() { releaseClip(); for (const item of previews.values())
            URL.revokeObjectURL(item.url); previews.clear(); },
    };
}
