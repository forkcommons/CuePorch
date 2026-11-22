const blockLabels = {
    navigation: "Navigation",
    hero: "Hero",
    upcoming: "Upcoming",
    on_demand: "On demand",
    agenda: "Agenda",
    faq: "FAQ",
};
const ALL = [
    "navigation", "hero", "upcoming", "on_demand", "agenda", "faq",
];
const KNOWN = new Set(ALL);
function editSections(list) {
    const rows = [];
    const makeRow = (block, enabled) => {
        const li = document.createElement("li");
        li.dataset.block = block;
        const id = `es-block-${block}`;
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.id = id;
        checkbox.checked = enabled;
        const label = document.createElement("label");
        label.htmlFor = id;
        label.textContent = blockLabels[block];
        const up = document.createElement("button");
        up.type = "button";
        up.textContent = "Up";
        up.setAttribute("aria-label", `Move ${blockLabels[block]} up`);
        const down = document.createElement("button");
        down.type = "button";
        down.textContent = "Down";
        down.setAttribute("aria-label", `Move ${blockLabels[block]} down`);
        li.append(checkbox, label, up, down);
        const row = { block, li, checkbox, up, down };
        checkbox.addEventListener("change", repartition);
        up.addEventListener("click", () => move(row, -1));
        down.addEventListener("click", () => move(row, 1));
        return row;
    };
    // Reorder existing <li> nodes in place; refocus so checkbox toggle keeps focus.
    const commit = () => {
        const doc = list.ownerDocument;
        const active = doc.activeElement;
        const keep = active instanceof HTMLElement && list.contains(active) ? active : null;
        for (const r of rows)
            list.appendChild(r.li);
        if (keep)
            keep.focus();
        sync();
    };
    const repartition = () => {
        rows.sort((a, b) => Number(b.checkbox.checked) - Number(a.checkbox.checked));
        commit();
    };
    const move = (row, dir) => {
        const i = rows.indexOf(row);
        const j = i + dir;
        if (j < 0 || j >= rows.length)
            return;
        if (rows[j].checkbox.checked !== row.checkbox.checked)
            return;
        [rows[i], rows[j]] = [rows[j], rows[i]];
        commit();
    };
    const sync = () => {
        const n = rows.length;
        rows.forEach((r, i) => {
            const enabled = r.checkbox.checked;
            const prev = rows[i - 1];
            const next = rows[i + 1];
            r.up.disabled = i === 0 || prev.checkbox.checked !== enabled;
            r.down.disabled = i === n - 1 || next.checkbox.checked !== enabled;
        });
    };
    const populate = (blocks) => {
        const chosen = new Set();
        const ordered = [];
        for (const b of blocks) {
            if (KNOWN.has(b) && !chosen.has(b)) {
                chosen.add(b);
                ordered.push(b);
            }
        }
        for (const b of ALL)
            if (!chosen.has(b))
                ordered.push(b);
        rows.length = 0;
        list.replaceChildren();
        for (const b of ordered) {
            const r = makeRow(b, chosen.has(b));
            rows.push(r);
            list.appendChild(r.li);
        }
        sync();
    };
    const order = () => rows.filter(r => r.checkbox.checked).map(r => r.block);
    return { populate, order };
}
function createSeriesDetails(root) {
    const navList = root.querySelector('#seriesNavigation');
    const faqList = root.querySelector('#seriesFaq');
    if (!navList || !faqList) {
        throw new Error('createSeriesDetails: #seriesNavigation and #seriesFaq are required');
    }
    const FAQ_LIMIT = 20;
    const addButton = (kind) => root.querySelector(`button[data-series-add="${kind}"]`);
    const faqRows = () => Array.from(faqList.querySelectorAll('[data-series-row="faq"]'));
    const syncFaqAddState = () => {
        const btn = addButton('faq');
        if (btn)
            btn.disabled = faqRows().length >= FAQ_LIMIT;
    };
    const makeRemove = (caption) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = 'Remove';
        btn.setAttribute('aria-label', caption);
        btn.addEventListener('click', () => {
            btn.closest('[data-series-row]')?.remove();
            syncFaqAddState();
        });
        return btn;
    };
    const makeNavRow = (label = '', href = '') => {
        const row = document.createElement('li');
        row.dataset.seriesRow = 'navigation';
        const labelWrap = document.createElement('label');
        labelWrap.textContent = 'Link label';
        const labelInput = document.createElement('input');
        labelInput.type = 'text';
        labelInput.name = 'seriesNavigationLabel';
        labelInput.maxLength = 100;
        labelInput.required = true;
        labelInput.value = label;
        labelWrap.appendChild(labelInput);
        const hrefWrap = document.createElement('label');
        hrefWrap.textContent = 'Link URL';
        const hrefInput = document.createElement('input');
        hrefInput.type = 'url';
        hrefInput.name = 'seriesNavigationHref';
        hrefInput.maxLength = 2048;
        hrefInput.required = true;
        hrefInput.value = href;
        hrefWrap.appendChild(hrefInput);
        row.append(labelWrap, hrefWrap, makeRemove('Remove navigation link'));
        return row;
    };
    const makeFaqRow = (question = '', answer = '') => {
        const row = document.createElement('li');
        row.dataset.seriesRow = 'faq';
        const qWrap = document.createElement('label');
        qWrap.textContent = 'Question';
        const qInput = document.createElement('textarea');
        qInput.name = 'seriesFaqQuestion';
        qInput.maxLength = 200;
        qInput.required = true;
        qInput.value = question;
        qWrap.appendChild(qInput);
        const aWrap = document.createElement('label');
        aWrap.textContent = 'Answer';
        const aInput = document.createElement('textarea');
        aInput.name = 'seriesFaqAnswer';
        aInput.maxLength = 1500;
        aInput.required = true;
        aInput.value = answer;
        aWrap.appendChild(aInput);
        row.append(qWrap, aWrap, makeRemove('Remove FAQ item'));
        return row;
    };
    const addNavRow = () => {
        const row = makeNavRow('', '');
        navList.appendChild(row);
        row.querySelector('input')?.focus();
    };
    const addFaqRow = () => {
        if (faqRows().length >= FAQ_LIMIT)
            return;
        const row = makeFaqRow('', '');
        faqList.appendChild(row);
        row.querySelector('textarea')?.focus();
        syncFaqAddState();
    };
    root.querySelectorAll('button[data-series-add]').forEach((btn) => {
        const kind = btn.dataset.seriesAdd;
        if (kind !== 'navigation' && kind !== 'faq')
            return;
        btn.type = 'button';
        btn.addEventListener('click', () => {
            if (kind === 'faq')
                addFaqRow();
            else
                addNavRow();
        });
    });
    const populate = (navigation, faq) => {
        navList.replaceChildren();
        faqList.replaceChildren();
        for (const link of navigation)
            navList.appendChild(makeNavRow(link.label, link.href));
        for (const item of faq.slice(0, FAQ_LIMIT)) {
            faqList.appendChild(makeFaqRow(item.question, item.answer));
        }
        syncFaqAddState();
    };
    const value = () => {
        const navigation = Array.from(navList.querySelectorAll('[data-series-row="navigation"]')).map((row) => ({
            label: row.querySelector('input[name="seriesNavigationLabel"]').value,
            href: row.querySelector('input[name="seriesNavigationHref"]').value,
        }));
        const faq = faqRows().map((row) => ({
            question: row.querySelector('textarea[name="seriesFaqQuestion"]').value,
            answer: row.querySelector('textarea[name="seriesFaqAnswer"]').value,
        }));
        return { navigation, faq };
    };
    syncFaqAddState();
    return { populate, value };
}
export function setupSeriesPresentation(root, upload) {
    const accent = root.querySelector('input[name="seriesAccent"]');
    const dark = root.querySelector('input[name="seriesDark"]');
    const dateLabel = root.querySelector('input[name="seriesDateLabel"]');
    const timezoneLabel = root.querySelector('input[name="seriesTimezoneLabel"]');
    const blocks = editSections(root.querySelector("#seriesBlocks"));
    const details = createSeriesDetails(root);
    const kinds = ["logo", "hero_image"];
    const saved = { logo: null, hero_image: null };
    const pending = { logo: null, hero_image: null };
    const images = {};
    for (const kind of kinds) {
        images[kind] = {
            input: root.querySelector(`[data-series-image="${kind}"]`),
            status: root.querySelector(`[data-series-image-status="${kind}"]`),
            clear: root.querySelector(`[data-series-clear="${kind}"]`),
        };
    }
    const resetImage = (kind) => {
        saved[kind] = null;
        pending[kind] = null;
        images[kind].input.value = "";
        images[kind].status.textContent = "No image selected";
    };
    for (const kind of kinds) {
        images[kind].input.addEventListener("change", () => {
            const file = images[kind].input.files?.[0] ?? null;
            pending[kind] = file;
            images[kind].status.textContent = file ? file.name : saved[kind] ? "Saved image" : "No image selected";
        });
        images[kind].clear.addEventListener("click", () => resetImage(kind));
    }
    const resolveImage = async (kind) => {
        const file = pending[kind];
        if (file) {
            saved[kind] = await upload(file);
            pending[kind] = null;
            images[kind].status.textContent = saved[kind] ? "Saved image" : "No image selected";
        }
        return saved[kind];
    };
    const populate = (p) => {
        accent.value = p.accent;
        dark.checked = p.dark;
        dateLabel.value = p.date_label;
        timezoneLabel.value = p.timezone_label;
        blocks.populate(p.blocks);
        details.populate(p.navigation, p.faq);
        for (const kind of kinds) {
            saved[kind] = p[kind];
            pending[kind] = null;
            images[kind].input.value = "";
            images[kind].status.textContent = p[kind] ? "Saved image" : "No image selected";
        }
    };
    const payload = async () => {
        const logo = await resolveImage("logo");
        const hero_image = await resolveImage("hero_image");
        const { navigation, faq } = details.value();
        return {
            blocks: blocks.order(),
            accent: accent.value,
            dark: dark.checked,
            logo,
            hero_image,
            navigation,
            date_label: dateLabel.value,
            timezone_label: timezoneLabel.value,
            faq,
        };
    };
    return { populate, payload };
}
