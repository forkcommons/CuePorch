export interface FolderRef {id: string; name: string}

export interface SlackSettings {
  configured: boolean; oauth_available: boolean; channel: string | null; revision: string | null; topics: string[];
  enabled: boolean; recursive: boolean; language: string;
  deliveries: {topic: string; state: string; attempts: number}[];
}

export async function editFolderSlack(
  folder: FolderRef,
  opts: {
    request: (path: string, method?: string, body?: object, headers?: Record<string,string>) => Promise<Response>;
    checked: (response: Response) => Promise<Response>;
    openDialog: (title: string) => HTMLElement;
    status: Pick<Node, "textContent">;
  }
) {
  const {request, checked, openDialog, status} = opts;
  const path = `/api/library/folders/${folder.id}/slack`;
  const settings: SlackSettings = await (await checked(await request(path))).json();
  const body = openDialog(`Slack notifications for ${folder.name}`);
  const note = document.createElement("p");
  note.textContent = settings.channel ? `Connected channel ${settings.channel}. Posts follow CuePorch access rules.`
    : settings.configured ? "Posts link to the team video and follow CuePorch access rules."
      : "Set the workspace HTTPS address to connect a Slack channel.";
  body.append(note);
  if (!settings.configured) return;
  const fields = document.createElement("form");
  fields.method = "post";
  const addressLabel = document.createElement("label");
  addressLabel.textContent = settings.revision ? "Replace webhook (optional)" : "Slack incoming webhook";
  const webhook = document.createElement("input");
  webhook.type = "password";
  webhook.required = !settings.revision;
  webhook.maxLength = 220;
  webhook.autocomplete = "off";
  addressLabel.append(webhook);
  fields.append(addressLabel);
  function setting(text: string, initial: boolean) {
    const label = document.createElement("label");
    const option = document.createElement("input");
    option.type = "checkbox";
    option.checked = initial;
    label.append(option, text);
    fields.append(label);
    return option;
  }
  const enabled = setting("Send Slack notifications", settings.enabled);
  const recursive = setting("Include subfolders", settings.recursive);
  const topics = {upload: "New uploads", comment: "Review comments", approval: "Approvals",
    edit: "Titles, thumbnails and removals", privacy: "Privacy changes", showcase: "Additions to showcases"};
  for (const [value, label] of Object.entries(topics)) {
    const wrapper = document.createElement("label");
    const option = document.createElement("input");
    option.type = "checkbox";
    option.name = "topics";
    option.value = value;
    option.checked = !settings.revision || settings.topics.includes(value);
    wrapper.append(option, label);
    fields.append(wrapper);
  }
  const label = document.createElement("label");
  label.textContent = "Message language";
  const language = document.createElement("select");
  language.add(new Option("English", "en"));
  language.add(new Option("Español", "es"));
  language.value = settings.language;
  label.append(language);
  fields.append(label);
  const save = document.createElement("button");
  save.textContent = "Save Slack settings";
  const disconnect = document.createElement("button");
  disconnect.type = "button";
  disconnect.textContent = "Disconnect Slack";
  disconnect.hidden = !settings.revision;
  const connect = document.createElement("button");
  connect.type = "button";
  connect.textContent = settings.channel ? "Change Slack channel" : "Choose channel in Slack";
  connect.hidden = !settings.oauth_available;
  fields.append(save, connect, disconnect);
  body.append(fields);
  const receipts = document.createElement("ul");
  receipts.setAttribute("aria-label", "Recent Slack deliveries");
  for (const entry of settings.deliveries) {
    const item = document.createElement("li");
    item.textContent = `${entry.topic === "delete" ? "Removed video" : topics[entry.topic as keyof typeof topics]} · ${entry.state}`;
    receipts.append(item);
  }
  body.append(receipts);
  function preferences() {
    return {
      revision: settings.revision, topics: new FormData(fields).getAll("topics"),
      enabled: enabled.checked, recursive: recursive.checked, language: language.value,
    };
  }
  async function store(method: "PUT" | "DELETE") {
    save.disabled = connect.disabled = disconnect.disabled = true;
    try {
      await checked(await request(path, method, {
        ...preferences(), webhook: webhook.value.trim(),
      }, {"X-CuePorch-Slack": "1"}));
      await editFolderSlack(folder, opts);
      status.textContent = method === "PUT" ? "Slack settings saved." : "Slack disconnected.";
    } catch (error) {
      status.textContent = String(error);
    } finally {
      save.disabled = connect.disabled = disconnect.disabled = false;
    }
  }
  fields.onsubmit = (event) => { event.preventDefault(); void store("PUT"); };
  disconnect.onclick = () => { void store("DELETE"); };
  connect.onclick = async () => {
    save.disabled = connect.disabled = disconnect.disabled = true;
    try {
      const response = await checked(await request(`${path}/connect`, "POST", preferences(), {"X-CuePorch-Slack": "1"}));
      const receipt: {authorization_url: string} = await response.json();
      location.assign(receipt.authorization_url);
    } catch (error) {
      status.textContent = String(error);
      save.disabled = connect.disabled = disconnect.disabled = false;
    }
  };
}
