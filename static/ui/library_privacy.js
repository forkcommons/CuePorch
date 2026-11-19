export async function openPrivacy(dialog) {
    const initial = await dialog.read();
    const body = dialog.open("Video privacy");
    const template = document.querySelector("#privacySettings");
    body.append(template.content.cloneNode(true));
    const form = body.querySelector("form");
    const visibility = form.querySelector("[name=visibility]");
    const password = form.querySelector("[name=password]");
    const embed = form.querySelector("[name=embed]");
    const domains = form.querySelector("[name=domains]");
    const download = form.querySelector("[name=download]");
    const save = form.querySelector("[type=submit]");
    const links = body.querySelector("[data-privacy-links]");
    let applied = initial;
    function toggleFields() {
        password.closest("label").hidden = visibility.value !== "password";
        password.placeholder = applied.has_password ? "Leave blank to keep the current password" : "";
        password.required = visibility.value === "password" && !applied.has_password;
        embed.disabled = download.disabled = visibility.value === "private";
        domains.closest("label").hidden = embed.disabled || embed.value !== "domains";
        domains.disabled = domains.closest("label").hidden;
    }
    function present(state) {
        applied = state;
        const displayed = state.pending || state;
        visibility.value = displayed.visibility;
        embed.value = displayed.embed;
        domains.value = displayed.domains.join("\n");
        download.checked = displayed.download;
        password.value = "";
        toggleFields();
        links.replaceChildren();
        for (const [label, path] of [["Watch link", state.watch], ["Embed link", state.embed_url]]) {
            if (!path)
                continue;
            const wrapper = document.createElement("label");
            wrapper.textContent = label;
            const input = document.createElement("input");
            input.type = "url";
            input.readOnly = true;
            input.value = dialog.absolute(path);
            wrapper.append(input);
            links.append(wrapper);
        }
        if (state.pending)
            dialog.status.textContent = "Waiting for the configured publication approval.";
    }
    visibility.onchange = embed.onchange = toggleFields;
    form.onsubmit = async (event) => {
        event.preventDefault();
        save.disabled = true;
        dialog.status.textContent = "";
        try {
            const state = await dialog.save({
                visibility: visibility.value,
                embed: embed.value,
                domains: domains.value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean),
                download: download.checked,
                password: visibility.value === "password" ? password.value : "",
            });
            present(state);
            if (!state.pending)
                dialog.status.textContent = "Privacy saved.";
            await dialog.reload();
        }
        catch (error) {
            dialog.status.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            save.disabled = false;
        }
    };
    present(initial);
}
