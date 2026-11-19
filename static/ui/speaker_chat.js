import { speakerRequest } from "./speaker_connection.js";
export function speakerChat(path, token) {
    const region = document.querySelector("#speakerPrivateChat");
    const form = region.querySelector("form");
    const value = form.elements.namedItem("message");
    const list = region.querySelector("ol");
    const status = region.querySelector("[role=status]");
    let pending = false;
    let rendered = "";
    let sending;
    async function refresh(body) {
        if (pending || region.hidden || !token())
            return;
        pending = true;
        try {
            const result = await speakerRequest(path + "/chat", token(), body ? "POST" : "GET", body);
            if (region.hidden)
                return;
            const stamp = result.messages.map(message => message.id).join(" ");
            if (stamp !== rendered) {
                rendered = stamp;
                list.replaceChildren(...result.messages.map(message => {
                    const row = document.createElement("li");
                    const who = document.createElement("strong");
                    who.textContent = message.author;
                    row.append(who, ` ${message.text}`);
                    return row;
                }));
            }
            status.textContent = "";
            if (body && body === sending) {
                sending = undefined;
                if (value.value === body.text)
                    value.value = "";
            }
        }
        catch (error) {
            status.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            pending = false;
            if (sending && sending !== body)
                void refresh(sending);
        }
    }
    form.onsubmit = event => {
        event.preventDefault();
        const text = value.value.trim();
        if (!text)
            return;
        if (sending?.text !== text)
            sending = { id: crypto.randomUUID().replaceAll("-", ""), text };
        void refresh(sending);
    };
    const timer = window.setInterval(() => { if (!document.hidden)
        void refresh(); }, 2500);
    return {
        show(visible) {
            if (region.hidden === !visible)
                return;
            region.hidden = !visible;
            if (visible)
                void refresh();
            else {
                sending = undefined;
                rendered = "";
                list.replaceChildren();
                value.value = "";
            }
        },
        close() { window.clearInterval(timer); },
    };
}
