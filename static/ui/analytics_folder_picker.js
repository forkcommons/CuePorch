function buildScope(folders, rootId) {
    const childrenMap = new Map();
    for (const [id, folder] of folders) {
        const parentId = folder.parent;
        if (parentId == null)
            continue;
        const siblings = childrenMap.get(parentId);
        if (siblings)
            siblings.push(id);
        else
            childrenMap.set(parentId, [id]);
    }
    const result = new Set([rootId]);
    const waiting = [rootId];
    for (let i = 0; i < waiting.length; i++) {
        const children = childrenMap.get(waiting[i]);
        if (!children)
            continue;
        for (let j = 0; j < children.length; j++) {
            const childId = children[j];
            if (result.has(childId))
                continue;
            result.add(childId);
            waiting.push(childId);
        }
    }
    return result;
}
export function setupAnalyticsFolders(root, onChange, addVideos, removeVideos) {
    const select = root.querySelector('#reportFolder');
    const addBtn = root.querySelector('#reportFolderAdd');
    const remBtn = root.querySelector('#reportFolderRemove');
    const countEl = root.querySelector('#reportFolderCount');
    let folders = new Map();
    let videos = [];
    let selected = null;
    let scope = new Set();
    let visible = [];
    // Human path, e.g. "Root / Child", with cycle guard.
    const pathOf = (id, seen = new Set()) => {
        const f = folders.get(id);
        if (!f || seen.has(id))
            return '';
        seen.add(id);
        const p = f.parent ? pathOf(f.parent, seen) : '';
        return p ? `${p} / ${f.name}` : f.name;
    };
    // All content (null) includes events; a folder includes its subtree only.
    const includes = (v) => selected === null ? true : !!v.folder && scope.has(v.folder);
    const refresh = (notify = true) => {
        scope = selected === null ? new Set() : buildScope(folders, selected);
        visible = selected === null ? [] : videos.filter(includes);
        countEl.textContent = selected === null
            ? ""
            : `${visible.length} video${visible.length === 1 ? "" : "s"}`;
        addBtn.disabled = visible.length === 0;
        remBtn.disabled = visible.length === 0;
        if (notify)
            onChange();
    };
    const populate = (fs, vs) => {
        folders = new Map(fs.map(folder => [folder.id, folder]));
        videos = vs;
        select.replaceChildren(new Option("All content", ""), ...fs.map(folder => new Option(pathOf(folder.id), folder.id)));
        selected = null;
        select.value = "";
        refresh(false);
    };
    select.addEventListener('change', () => {
        selected = select.value || null;
        refresh();
    });
    addBtn.addEventListener('click', () => addVideos(visible.map(v => v.id)));
    remBtn.addEventListener('click', () => removeVideos(visible.map(v => v.id)));
    return { populate, includes };
}
