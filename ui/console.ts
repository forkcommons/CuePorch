const key = document.querySelector<HTMLInputElement>("input#key")!;
key.value = sessionStorage.getItem("cueporchOperator") || "";
const load = document.querySelector<HTMLButtonElement>("button#load")!;
const settings = document.querySelector<HTMLElement>("div#settings")!;
const queue = document.querySelector<HTMLElement>("div#queue")!;
const result = document.querySelector<HTMLElement>("p#result")!;
const call = (url: string, opt: RequestInit = {}) =>
  fetch(url, {
    ...opt,
    headers: {
      Authorization: "Bearer " + key.value,
      "Content-Type": "application/json",
    },
  });
load.onclick = async () => {
  const response = await call("/api/policy");
  if (!response.ok) {
    result.textContent = "Access denied";
    return;
  }
  settings.replaceChildren();
  for (const [step, required] of Object.entries(await response.json())) {
    const label = document.createElement("label"),
      input = document.createElement("input");
    input.type = "checkbox";
    input.checked = required === true;
    input.onchange = async () => {
      let r = await call("/api/policy/" + step, {
        method: "PUT",
        body: JSON.stringify({ required: input.checked }),
      });
      result.textContent = r.ok ? "Saved" : "Unable to save";
      if (!r.ok) input.checked = !input.checked;
    };
    label.append(
      input,
      document.createTextNode("Require approval for " + step),
    );
    settings.append(label, document.createElement("br"));
  }
  queue.replaceChildren();
  for (const job of await (await call("/api/actions")).json()) {
    const line = document.createElement("p");
    line.append(document.createTextNode(job.step + " " + job.payload + " "));
    for (const decision of ["approve", "reject"]) {
      const button = document.createElement("button");
      button.textContent = decision;
      button.onclick = async () => {
        const r = await call("/api/actions/" + job.id + "/decision", {
          method: "POST",
          body: JSON.stringify({ decision }),
        });
        if (r.ok) load.click();
        else result.textContent = "Decision failed";
      };
      line.append(button);
    }
    queue.append(line);
  }
};
