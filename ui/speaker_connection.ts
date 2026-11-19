export async function speakerRequest<T>(endpoint: string, token: string, method = "GET", body?: object): Promise<T> {
  const headers: Record<string, string> = {Authorization: `Bearer ${token}`};
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(endpoint, {method, headers, cache: "no-store", signal: AbortSignal.timeout(8000),
    body: body ? JSON.stringify(body) : undefined});
  if (!response.ok) {
    const reason = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
    throw new SpeakerRequestError(reason || `Speaker connection returned ${response.status}.`, response.status);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export class SpeakerRequestError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function gather(connection: RTCPeerConnection, description: RTCSessionDescriptionInit) {
  await connection.setLocalDescription(description);
  if (connection.iceGatheringState !== "complete") {
    await new Promise<void>(resolve => {
      const finished = () => {
        window.clearTimeout(timer);
        connection.removeEventListener("icegatheringstatechange", check);
        resolve();
      };
      const check = () => { if (connection.iceGatheringState === "complete") finished(); };
      const timer = window.setTimeout(finished, 8000);
      connection.addEventListener("icegatheringstatechange", check);
      check();
    });
  }
  if (!connection.localDescription) throw new Error("The media connection closed during setup.");
  return connection.localDescription.sdp;
}
