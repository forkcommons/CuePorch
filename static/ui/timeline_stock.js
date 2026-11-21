import { uploadLibraryFile } from "./library_upload.js";
import { privateSettings } from "./library_privacy.js";
import { mountTimelineStockCatalog } from "./timeline_stock_catalog.js";
function el(id) {
    const node = document.getElementById(id);
    if (!node)
        throw new Error(`Missing element #${id}`);
    return node;
}
export function mountTimelineStock(context) {
    const search = el("timelineStockSearch");
    const category = el("timelineStockCategory");
    const kind = el("timelineStockKind");
    const mood = el("timelineStockMood");
    const genre = el("timelineStockGenre");
    const assetSelect = el("timelineStockAsset");
    const credit = el("timelineStockCredit");
    const useButton = el("timelineStockUse");
    const status = el("timelineStockStatus");
    const uploadForm = el("timelineStockUpload");
    const fileInput = el("timelineStockFile");
    const uploadCategory = el("timelineStockUploadCategory");
    const uploadMood = el("timelineStockUploadMood");
    const uploadGenre = el("timelineStockUploadGenre");
    const license = el("timelineStockLicense");
    const author = el("timelineStockAuthor");
    const source = el("timelineStockSource");
    const permission = el("timelineStockPermission");
    const uploadButton = el("timelineStockUploadButton");
    let uploadId = "";
    let uploadSignature = "";
    let uploading = false;
    const stockItems = () => context.items().filter((item) => !!item.stock_asset);
    const usableItems = () => stockItems().filter((item) => item.asset_type === "video"
        || item.asset_type === "audio"
        || (item.asset_type === "image" && !!item.source_cut));
    function unique(key) {
        const values = usableItems()
            .map((item) => String(item.stock_asset?.[key] ?? "").trim())
            .filter(Boolean);
        return Array.from(new Set(values)).sort((a, b) => a.localeCompare(b));
    }
    function fill(select, values, first) {
        const current = select.value;
        select.replaceChildren();
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = first;
        select.append(empty);
        for (const value of values) {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            select.append(option);
        }
        select.value = values.includes(current) ? current : "";
    }
    function selectedItem() {
        return context.items().find((item) => item.id === assetSelect.value && item.stock_asset);
    }
    function syncSelected() {
        const item = selectedItem();
        useButton.disabled = !item;
        if (!item?.stock_asset) {
            credit.textContent = "";
            return;
        }
        const rights = item.stock_asset;
        credit.textContent = [
            rights.credit ? `Credit: ${rights.credit}` : "",
            rights.license ? `License: ${rights.license}` : "",
            rights.source_url ? `Source: ${rights.source_url}` : "",
        ].filter(Boolean).join("\n");
    }
    function filteredItems() {
        const q = search.value.trim().toLowerCase();
        const c = category.value;
        const k = kind.value;
        const m = mood.value;
        const g = genre.value;
        return usableItems().filter((item) => {
            const rights = item.stock_asset;
            if (k && item.asset_type !== k)
                return false;
            if (c && rights.category !== c)
                return false;
            if (m && rights.mood !== m)
                return false;
            if (g && rights.genre !== g)
                return false;
            if (!q)
                return true;
            return [item.title, rights.credit, rights.category, rights.mood, rights.genre, rights.license]
                .some((value) => String(value).toLowerCase().includes(q));
        });
    }
    function renderAssets() {
        const list = filteredItems();
        const current = assetSelect.value;
        assetSelect.replaceChildren();
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = list.length ? "Select an asset" : "No matches";
        assetSelect.append(empty);
        for (const item of list) {
            const option = document.createElement("option");
            option.value = item.id;
            option.textContent = `${item.title} (${item.asset_type})`;
            assetSelect.append(option);
        }
        assetSelect.disabled = !list.length;
        assetSelect.value = list.some((item) => item.id === current) ? current : "";
        syncSelected();
    }
    function rebuild() {
        fill(category, unique("category"), "All categories");
        fill(kind, ["video", "audio", "image"], "All kinds");
        fill(mood, unique("mood"), "All moods");
        fill(genre, unique("genre"), "All genres");
        renderAssets();
    }
    function showItem(id) {
        rebuild();
        const visible = Array.from(assetSelect.options).some((option) => option.value === id);
        if (!visible) {
            search.value = "";
            category.value = "";
            kind.value = "";
            mood.value = "";
            genre.value = "";
            rebuild();
        }
        if (Array.from(assetSelect.options).some((option) => option.value === id)) {
            assetSelect.value = id;
            syncSelected();
        }
    }
    search.addEventListener("input", renderAssets);
    category.addEventListener("change", renderAssets);
    kind.addEventListener("change", renderAssets);
    mood.addEventListener("change", renderAssets);
    genre.addEventListener("change", renderAssets);
    assetSelect.addEventListener("change", syncSelected);
    useButton.addEventListener("click", async () => {
        const item = selectedItem();
        if (!item)
            return;
        useButton.disabled = true;
        status.textContent = "Using asset…";
        try {
            await context.use(item);
            status.textContent = "Asset selected.";
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : "Could not use asset.";
        }
        finally {
            useButton.disabled = !selectedItem();
        }
    });
    uploadForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (uploading)
            return;
        const file = fileInput.files?.[0];
        if (!file) {
            status.textContent = "Choose a file to upload.";
            return;
        }
        if (license.value === "permission" && !permission.value.trim()) {
            status.textContent = "Describe the permission you have to upload this asset.";
            return;
        }
        if (license.value !== "permission" && !source.value.trim()) {
            status.textContent = "Provide a source URL for this license.";
            return;
        }
        const signature = JSON.stringify([
            file.name, file.size, file.lastModified, file.type,
            uploadCategory.value, uploadMood.value, uploadGenre.value,
            license.value, author.value, source.value, permission.value,
        ]);
        if (!uploadId || signature !== uploadSignature) {
            uploadId = crypto.randomUUID();
            uploadSignature = signature;
        }
        uploading = true;
        uploadButton.disabled = true;
        status.textContent = "Uploading…";
        try {
            const stockAsset = {
                category: uploadCategory.value,
                mood: uploadMood.value,
                genre: uploadGenre.value,
                credit: author.value.trim(),
                source_url: source.value.trim(),
                license: license.value.trim(),
                permission_note: permission.value.trim(),
            };
            const receipt = await uploadLibraryFile(file, uploadId, context.folder(), context.credential(), privateSettings, (value) => {
                status.textContent = value == null
                    ? "Uploading…"
                    : `Uploading ${Math.max(0, Math.min(100, Math.round(value)))}%`;
            }, "/api/library/uploads", stockAsset);
            const savedId = receipt.id;
            await context.refresh();
            uploadForm.reset();
            uploadId = "";
            uploadSignature = "";
            status.textContent = "Uploaded.";
            showItem(savedId);
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : "Upload failed.";
        }
        finally {
            uploading = false;
            uploadButton.disabled = false;
        }
    });
    mountTimelineStockCatalog({ credential: context.credential, folder: context.folder, async available(id) { await context.refresh(); showItem(id); } });
    rebuild();
    return { rebuild };
}
