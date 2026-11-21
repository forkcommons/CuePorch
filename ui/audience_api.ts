export function viewerRequest(path: string, method: "GET" | "POST" | "PUT" | "DELETE" = "GET", body: object = {}) {
  return fetch("/api/viewer/" + path, {
    method, cache: "no-store", credentials: "same-origin",
    ...(method === "GET" ? {} : {
      headers: {"Content-Type": "application/json", "X-CuePorch-Viewer": "1"},
      body: JSON.stringify(body),
    }),
  });
}

export function viewerVideo(node: HTMLElement) {
  const kind = node.dataset.kind || "";
  const slug = node.dataset.slug || "";
  const path = `${encodeURIComponent(kind)}/${encodeURIComponent(slug)}`;
  const next = node.dataset.next || `/watch/${path}`;
  return {kind, slug, path, account: "/account?" + new URLSearchParams({next})};
}
