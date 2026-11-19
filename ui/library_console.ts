const key = document.querySelector<HTMLInputElement>("input#key")!;
key.value = sessionStorage.getItem("cueporchOperator") || "";
const load = document.querySelector<HTMLButtonElement>("button#load")!;
const exportMap =
  document.querySelector<HTMLButtonElement>("button#exportMap")!;
const publishImports = document.querySelector<HTMLButtonElement>(
  "button#publishImports",
)!;
const items = document.querySelector<HTMLTableSectionElement>("tbody#items")!;
const result = document.querySelector<HTMLElement>("p#result")!;
const headers = () => ({
  Authorization: "Bearer " + key.value,
  "Content-Type": "application/json",
});
publishImports.onclick = async () => {
  const response = await fetch("/api/migration/publish-eligible", {
    method: "POST",
    headers: headers(),
  });
  result.textContent = response.ok
    ? "Queued eligible imports: " + (await response.json()).queued
    : "Unable to queue imports";
};
exportMap.onclick = async () => {
  const r = await fetch("/api/embed-map.csv", { headers: headers() });
  if (!r.ok) return;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await r.blob());
  link.download = "cueporch-embed-map.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
};
load.onclick = async () => {
  const response = await fetch("/api/library", { headers: headers() });
  if (!response.ok) {
    result.textContent = "Unable to load library";
    return;
  }
  items.replaceChildren();
  for (const work of await response.json()) {
    const row = items.insertRow();
    row.insertCell().textContent = work.title;
    row.insertCell().textContent = [
      work.quality,
      ...(work.exceptions || []).map((x: unknown) =>
        typeof x === "string" ? x : JSON.stringify(x),
      ),
    ].join(" · ");
    row.insertCell().textContent =
      [work.delivery?.status, work.delivery?.error]
        .filter(Boolean)
        .join(" · ") || "Preserved locally";
    const actions = row.insertCell();
    for (const pub of work.public_eligible ? [false, true] : [false]) {
      const btn = document.createElement("button");
      btn.textContent = pub ? "Publish publicly" : "Deliver privately";
      btn.onclick = async () => {
        const r = await fetch("/api/delivery/" + work.id, {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({ public: pub }),
        });
        result.textContent = r.ok
          ? "Queued " + work.title
          : "Request failed (" + r.status + ")";
      };
      actions.append(btn);
    }
    if (work.delivery?.route_kind) {
      const link = document.createElement("a");
      link.href = "/watch/" + work.delivery.route_kind + "/" + work.id;
      link.textContent = "Watch";
      actions.append(link);
    }
  }
};
