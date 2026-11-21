export const sessionMarker = "team-session";
export const roleLabels = {
    owner: "Owner", admin: "Administrator", contributor_plus: "Contributor Plus", contributor: "Contributor", viewer: "Viewer",
};
export async function teamRequest(path, method = "GET", body = {}, credential = "") {
    const headers = {};
    if (credential)
        headers.Authorization = `Bearer ${credential}`;
    if (method !== "GET") {
        headers["Content-Type"] = "application/json";
        headers["X-CuePorch-Team"] = "1";
    }
    return fetch(`/api/team/${path}`, { method, headers, body: method === "GET" ? undefined : JSON.stringify(body) });
}
export async function checked(response) {
    if (response.ok)
        return response;
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    throw new Error(page.querySelector("p")?.textContent || `Team request failed (${response.status}).`);
}
