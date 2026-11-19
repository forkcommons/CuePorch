export async function speakerRequest(endpoint, token, method = "GET", body) {
    const headers = { Authorization: `Bearer ${token}` };
    if (body)
        headers["Content-Type"] = "application/json";
    const response = await fetch(endpoint, { method, headers, cache: "no-store", signal: AbortSignal.timeout(8000),
        body: body ? JSON.stringify(body) : undefined });
    if (!response.ok) {
        const reason = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
        throw new SpeakerRequestError(reason || `Speaker connection returned ${response.status}.`, response.status);
    }
    return response.status === 204 ? undefined : response.json();
}
export class SpeakerRequestError extends Error {
    status;
    constructor(message, status) {
        super(message);
        this.status = status;
    }
}
export async function gather(connection, description) {
    await connection.setLocalDescription(description);
    if (connection.iceGatheringState !== "complete") {
        await new Promise(resolve => {
            const finished = () => {
                window.clearTimeout(timer);
                connection.removeEventListener("icegatheringstatechange", check);
                resolve();
            };
            const check = () => { if (connection.iceGatheringState === "complete")
                finished(); };
            const timer = window.setTimeout(finished, 8000);
            connection.addEventListener("icegatheringstatechange", check);
            check();
        });
    }
    if (!connection.localDescription)
        throw new Error("The media connection closed during setup.");
    return connection.localDescription.sdp;
}
