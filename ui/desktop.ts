const field = (id: string) => document.getElementById(id) as HTMLInputElement;
const token = location.hash.slice(1) || sessionStorage.getItem("desktop") || "";
history.replaceState(null, "", location.pathname);
sessionStorage.setItem("desktop", token);
const headers = {
  Authorization: "Bearer " + token,
  "Content-Type": "application/json",
};
const message = document.getElementById("message")!;
const setup = document.querySelector("form")!;
async function refresh() {
  const response = await fetch("/desktop/api/status", { headers });
  if (!response.ok) {
    message.textContent =
      "Open CuePorch from the application shortcut to sign in.";
    return;
  }
  const data: {
    institution?: string;
    operator?: string;
    origin?: string;
    data: string;
  } = await response.json();
  document.getElementById("data")!.textContent = "Library folder: " + data.data;
  if (data.institution && data.operator) {
    sessionStorage.setItem("cueporchOperator", data.operator);
    document.getElementById("ready")!.hidden = false;
    field("name").value = data.institution;
    field("name").readOnly = true;
    document.getElementById("org")!.textContent = data.institution;
    document.getElementById("hosting")!.textContent = data.origin
      ? "Publishing to " + data.origin
      : "Local library ready. Connect a host to publish publicly.";
    setup.hidden = Boolean(data.origin);
  }
}
setup.onsubmit = async (event) => {
  event.preventDefault();
  const response = await fetch("/desktop/api/setup", {
    method: "POST",
    headers,
    body: JSON.stringify({
      institution: field("name").value,
      origin: field("origin").value,
      username: field("username").value,
      password: field("password").value,
    }),
  });
  if (!response.ok) {
    message.textContent =
      "Setup failed. Check the institution name, video host and credentials.";
    return;
  }
  field("password").value = "";
  message.textContent = "Setup saved.";
  await refresh();
};
document.getElementById("quit")!.onclick = async () => {
  if ((await fetch("/desktop/api/quit", { method: "POST", headers })).ok)
    message.textContent = "CuePorch has closed. You can close this tab.";
};
void refresh();
