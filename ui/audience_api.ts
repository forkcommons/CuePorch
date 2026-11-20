export function viewerRequest(path: string, method: "GET" | "POST" | "PUT" | "DELETE" = "GET", body: object = {}) {
  return fetch("/api/viewer/" + path, {
    method, cache: "no-store", credentials: "same-origin",
    ...(method === "GET" ? {} : {
      headers: {"Content-Type": "application/json", "X-CuePorch-Viewer": "1"},
      body: JSON.stringify(body),
    }),
  });
}
