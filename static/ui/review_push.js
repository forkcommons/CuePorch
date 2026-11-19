export async function configureReviewPush(endpoint, status) {
    if (!("serviceWorker" in navigator && "PushManager" in window && "Notification" in window))
        return;
    const button = document.querySelector("#reviewPushToggle");
    const url = endpoint + "/push";
    const response = await fetch(url);
    if (!response.ok)
        return;
    const settings = await response.json();
    if (!settings.enabled)
        return;
    const registration = await navigator.serviceWorker.register("/review/notifications-worker.js", {
        scope: "/review/", type: "module",
    });
    const showState = () => {
        button.textContent = settings.subscribed ? "Turn off device notifications" : "Enable device notifications";
    };
    button.hidden = false;
    showState();
    button.onclick = async () => {
        button.disabled = true;
        try {
            if (settings.subscribed) {
                const result = await fetch(url, { method: "DELETE" });
                if (!result.ok)
                    throw new Error("Could not turn off device notifications.");
                settings.subscribed = false;
            }
            else {
                if (await Notification.requestPermission() !== "granted") {
                    throw new Error("Allow notifications in your browser to receive device alerts.");
                }
                await navigator.serviceWorker.ready;
                const subscription = await registration.pushManager.getSubscription()
                    ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: settings.key });
                const result = await fetch(url, {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(subscription.toJSON()),
                });
                if (!result.ok) {
                    throw new Error("Could not enable device notifications with this browser's push service.");
                }
                settings.subscribed = true;
            }
            status.textContent = settings.subscribed ? "Device notifications enabled." : "Device notifications disabled.";
            showState();
        }
        catch (error) {
            status.textContent = String(error);
        }
        finally {
            button.disabled = false;
        }
    };
}
