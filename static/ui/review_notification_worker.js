const worker = globalThis;
function reviewPath(data) {
    if (!data || typeof data !== "object" || !("path" in data) || typeof data.path !== "string")
        return null;
    return /^\/review\/[A-Za-z0-9_-]{43}\?version=[0-9a-f]{64}#comment-[0-9a-f]{32}$/.test(data.path)
        ? data.path
        : null;
}
worker.addEventListener("push", (event) => {
    let path;
    try {
        path = reviewPath(event.data?.json());
    }
    catch {
        return;
    }
    if (!path)
        return;
    event.waitUntil(worker.registration.showNotification("New CuePorch review feedback", {
        body: "Open the review to read the comment.",
        tag: path.split("#")[0],
        data: { path },
    }));
});
worker.addEventListener("notificationclick", (event) => {
    const path = reviewPath(event.notification.data);
    event.notification.close();
    if (path)
        event.waitUntil(worker.clients.openWindow(path));
});
export {};
