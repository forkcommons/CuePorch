const message = document.querySelector("p#message");
const v = document.querySelector("video");
let stopped = false;
function closeScreen() {
    if (stopped)
        return;
    stopped = true;
    v.pause();
    v.removeAttribute("src");
    v.load();
    message.textContent = "This screening has closed.";
}
const expires = setTimeout(closeScreen, Number(v.dataset.remaining) * 1000);
async function check() {
    if (stopped)
        return;
    try {
        let r = await fetch(location.pathname + "/status", {
            cache: "no-store",
            signal: AbortSignal.timeout(2000),
        });
        if (!r.ok) {
            closeScreen();
            return;
        }
    }
    catch {
        closeScreen();
        return;
    }
    setTimeout(check, 1000);
}
check();
export {};
