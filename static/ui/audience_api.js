export function viewerRequest(path, method = "GET", body = {}) {
    return fetch("/api/viewer/" + path, {
        method, cache: "no-store", credentials: "same-origin",
        ...(method === "GET" ? {} : {
            headers: { "Content-Type": "application/json", "X-CuePorch-Viewer": "1" },
            body: JSON.stringify(body),
        }),
    });
}
