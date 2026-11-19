interface Started { token: string }

export class BrowserBroadcast {
  private tail = Promise.resolve();
  private counter = 0;
  private pending = 0;
  private interrupted = false;
  private readonly url: string;

  static async open(path: string, credential: string, failed: () => void) {
    const response = await fetch(path, {method: "POST", headers: {Authorization: "Bearer " + credential},
      signal: AbortSignal.timeout(35000)});
    if (!response.ok) {
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      throw new Error(page.querySelector("p")?.textContent || "Could not start the browser broadcast.");
    }
    const {token}: Started = await response.json();
    return new BrowserBroadcast(path + "/" + encodeURIComponent(token), credential, failed);
  }

  private constructor(url: string, private credential: string, private failed: () => void) {
    this.url = url;
  }

  add(data: Blob) {
    if (this.interrupted) return;
    if (this.pending >= 8) {
      this.fail();
      return;
    }
    const sequence = this.counter++;
    this.pending += 1;
    this.tail = this.tail.then(async () => {
      if (!this.interrupted) await this.send(sequence, data);
    }).catch(() => this.fail()).finally(() => {
      this.pending -= 1;
    });
  }

  private async send(sequence: number, data: Blob) {
    for (let attempt = 0; attempt < 4; attempt += 1) {
      let response: Response | undefined;
      try {
        response = await fetch(this.url + "/" + sequence, {method: "PUT", body: data,
          headers: {Authorization: "Bearer " + this.credential},
          signal: AbortSignal.timeout(4000)});
      } catch {
        // The server can deduplicate a repeat of the preceding chunk.
      }
      if (response?.ok) return;
      if (response && response.status !== 429 && response.status < 500) break;
      await new Promise(resolve => window.setTimeout(resolve, 250 * (attempt + 1)));
    }
    throw new Error("Browser broadcast intake is unavailable.");
  }

  async finish() {
    await this.tail;
    await this.close();
  }

  close() {
    return fetch(this.url, {method: "DELETE", keepalive: true,
      headers: {Authorization: "Bearer " + this.credential}}).catch(() => undefined);
  }

  private fail() {
    if (this.interrupted) return;
    this.interrupted = true;
    this.failed();
    void this.close();
  }
}
