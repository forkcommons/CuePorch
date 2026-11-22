import { saveEmbedMap } from "./embed_map.js";

const key = document.querySelector<HTMLInputElement>("input#key")!;
key.value = sessionStorage.getItem("cueporchOperator") || "";
const source = document.querySelector<HTMLInputElement>("input#source")!;
const inventory =
  document.querySelector<HTMLButtonElement>("button#inventory")!;
const transfer = document.querySelector<HTMLButtonElement>("button#transfer")!;
const rows = document.querySelector<HTMLTableSectionElement>("tbody#rows")!;
const folderIssue = document.querySelector<HTMLElement>("#migrationFolderIssue")!;
const folderDetails = document.querySelector<HTMLDetailsElement>("#migrationFolders")!;
const folderSummary = document.querySelector<HTMLElement>("#migrationFolderSummary")!;
const folderList = document.querySelector<HTMLUListElement>("#migrationFolderList")!;
const showcases = document.querySelector<HTMLElement>("#showcaseInventory")!;
const showcaseIssue = document.querySelector<HTMLElement>("#showcaseInventoryIssue")!;
const showcaseRows = document.querySelector<HTMLTableSectionElement>("#showcaseInventoryRows")!;
const cutover = document.querySelector<HTMLElement>("#migrationCutover")!;
const exportMap = document.querySelector<HTMLButtonElement>("#migrationExportMap")!;
const cutoverStatus = document.querySelector<HTMLElement>("#migrationCutoverStatus")!;
const summary = document.querySelector<HTMLElement>("#migrationSummary")!;
const pagination = document.querySelector<HTMLElement>("#migrationPages")!;
const pageNumber = document.querySelector<HTMLElement>("#migrationPageNumber")!;
const previousPage = document.querySelector<HTMLButtonElement>("#migrationPreviousPage")!;
const nextPage = document.querySelector<HTMLButtonElement>("#migrationNextPage")!;
let currentPage = 1;
const mediaIssueLabels: Record<string, string> = {
  incomplete_media: "Incomplete download",
  invalid_source_checksum: "Invalid Vimeo checksum",
  source_checksum_mismatch: "Download did not match the Vimeo checksum",
  media_connection_interrupted: "Media connection interrupted",
};
const headers = () => ({
  Authorization: "Bearer " + key.value,
  "Content-Type": "application/json",
});
async function run(dry: boolean) {
  inventory.disabled = transfer.disabled = true;
  cutover.hidden = true;
  cutoverStatus.textContent = "";
  summary.textContent = "";
  pagination.hidden = true;
  currentPage = 1;
  statusBox.textContent = "Starting migration request…";
  try {
    const response = await fetch("/api/migration", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ dry_run: dry, token: source.value }),
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    await poll();
  } catch (error) {
    failed(error);
  }
}
const statusBox = document.querySelector("p#status")!;
function failed(error: unknown) {
  inventory.disabled = transfer.disabled = false;
  statusBox.textContent = error instanceof Error ? error.message : String(error);
}
async function poll(page = currentPage) {
  const r = await fetch("/api/migration?page=" + page, { headers: headers() });
  if (!r.ok) throw new Error(`Could not read migration progress (${r.status})`);
  const data = await r.json();
  const active = data.state === "running" || data.state === "waiting";
  inventory.disabled = transfer.disabled = active;
  cutover.hidden = data.state !== "transferred";
  statusBox.textContent = data.state + (data.detail ? " · " + data.detail : "");
  if (data.counts) {
    const dispositions = Object.entries(data.counts).map(([name, count]) => `${count} ${name}`);
    summary.textContent = [`${data.total} videos`, ...dispositions,
      `${data.metadata_incomplete} with unavailable source metadata`,
      ...(data.transfer_incomplete ? [`${data.transfer_incomplete} with media errors`] : []),
    ].join(" · ");
  } else {
    summary.textContent = typeof data.processed === "number"
      ? `${data.processed} of ${data.total} videos processed` : "";
  }
  currentPage = data.page || 1;
  pagination.hidden = !data.pages || data.pages < 2;
  pageNumber.textContent = `Page ${currentPage} of ${data.pages || 1}`;
  previousPage.disabled = currentPage <= 1;
  nextPage.disabled = currentPage >= (data.pages || 1);
  rows.replaceChildren();
  for (const w of data.works || []) {
    const row = rows.insertRow();
    const folder = w.source_folder_path ?? (w.source_folder_unavailable ? "Folder unavailable" : "Unfiled");
    for (const v of [w.title, w.source_id, folder, w.disposition, w.source_privacy])
      row.insertCell().textContent = v;
    type TextTrackRecovery = { language?: string; kind?: string; state: string; issue?: string };
    const versions: { uri?: string; number?: number; state: string; issue?: string; text_tracks?: TextTrackRecovery[] }[] = w.version_recovery || [];
    const previousCuts = row.insertCell();
    if (!versions.length) {
      previousCuts.textContent = w.unavailable_metadata?.["/versions"] ? "Unavailable" : "None reported";
    } else {
      const needed = versions.filter(version => version.state === "owner_file_needed").length;
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${versions.length} previous ${versions.length === 1 ? "cut" : "cuts"}` +
        (needed ? ` · ${needed} ${needed === 1 ? "needs a file" : "need files"}` : "");
      const list = document.createElement("ul");
      for (const version of versions) {
        const item = document.createElement("li");
        const label = typeof version.number === "number" ? `Vimeo version ${version.number}` :
          version.uri ? `Vimeo version ${version.uri.split("/").pop()}` : "Previous Vimeo cut";
        const status = version.state === "recovered" ? "Recovered" :
          version.state === "available" ? "Available to transfer" :
          version.state === "retained" ? "Already in Library" : "Owner file needed";
        item.textContent = [label, status, version.issue].filter(Boolean).join(" · ");
        if (version.text_tracks?.length) {
          const trackList = document.createElement("ul");
          const statusLabels: Record<string, string> = {
            available: "Available to transfer",
            recovered: "Recovered",
            owner_file_needed: "Owner file needed",
            inventory_unavailable: "Source inventory unavailable",
            disabled: "Disabled on Vimeo",
          };
          for (const track of version.text_tracks) {
            const trackItem = document.createElement("li");
            trackItem.textContent = [
              track.language,
              track.kind,
              statusLabels[track.state] ?? track.state,
              track.issue,
            ].filter(Boolean).join(" · ");
            trackList.append(trackItem);
          }
          item.append(trackList);
        }
        list.append(item);
      }
      details.append(summary, list);
      previousCuts.append(details);
    }
    const audioRecovery = ((w.audio_recovery ?? []) as {version_uri:string;language?:string;kind?:string;state:string;issue?:string}[]);
    const audioCell = row.insertCell();
    if (!audioRecovery.length) {
      audioCell.textContent = "None reported";
    } else {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${audioRecovery.length} audio result${audioRecovery.length === 1 ? "" : "s"}`;
      details.appendChild(summary);
      const stateLabels: Record<string, string> = { available: "Available to transfer", recovered: "Recovered", disabled: "Disabled on Vimeo", owner_file_needed: "Owner file needed", inventory_unavailable: "Source inventory unavailable" };
      for (const audio of audioRecovery) {
        const line = document.createElement("div");
        const lastSegment = audio.version_uri.split("/").pop() ?? audio.version_uri;
        line.textContent = [`Vimeo version ${lastSegment}`, audio.language, audio.kind, stateLabels[audio.state] ?? audio.state, audio.issue].filter(Boolean).join(" · ");
        details.appendChild(line);
      }
      audioCell.appendChild(details);
    }
    const review = w.source_review;
    const reviewCell = row.insertCell();
    reviewCell.textContent = review?.state === "available"
      ? (review.links.length
        ? `${review.links.length} Vimeo review ${review.links.length === 1 ? "link" : "links"}`
        : "None reported")
      : "Source settings unavailable";
    const reviewStatuses: Record<string, string> = {
      approved: "Approved", needs_review: "Needs review", in_progress: "In progress",
    };
    if (review?.status && reviewStatuses[review.status]) {
      reviewCell.textContent = `${reviewStatuses[review.status]} · ${reviewCell.textContent}`;
    }
    const exceptions = [
      ...Object.entries(w.unavailable_metadata || {}).map(([resource, status]) => `${resource.slice(1)} (${status})`),
      ...(w.transfer_issues || []).map((issue: {role: string; http_status?: number; reason?: string}) =>
        `${issue.role} (${issue.reason ? mediaIssueLabels[issue.reason] || "Transfer failed" : `HTTP ${issue.http_status}`})`),
    ];
    row.insertCell().textContent = exceptions.join(", ") || "None reported";
  }
  const folders = data.folders || [];
  folderIssue.textContent = data.folder_issue || "";
  folderIssue.hidden = !data.folder_issue;
  folderDetails.hidden = !folders.length;
  folderSummary.textContent = `${folders.length} source ${folders.length === 1 ? "folder" : "folders"}`;
  folderList.replaceChildren();
  for (const folder of folders) {
    const item = document.createElement("li");
    item.textContent = folder.path;
    folderList.append(item);
  }
  const albums = data.showcases || [];
  showcases.hidden = !albums.length && !data.showcase_issue;
  showcaseIssue.textContent = data.showcase_issue || "";
  showcaseRows.replaceChildren();
  for (const album of albums) {
    const row = showcaseRows.insertRow();
    for (const value of [album.title, album.source_visibility, String(album.members.length), album.missing.join(", ")]) {
      row.insertCell().textContent = value;
    }
    const target = row.insertCell();
    if (album.target) {
      const link = document.createElement("a");
      link.href = "/operator/showcases?select=" + encodeURIComponent(album.target);
      link.textContent = "Open showcase";
      target.append(link);
    } else {
      target.textContent = data.state === "transferred" ? "Removed locally" : "Private on transfer";
    }
    if (album.issues.length) target.append(" " + album.issues.join(" "));
  }
  if (active) setTimeout(() => void poll().catch(failed), 750);
}
inventory.onclick = () => run(true);
transfer.onclick = () => run(false);
previousPage.onclick = () => void poll(currentPage - 1).catch(failed);
nextPage.onclick = () => void poll(currentPage + 1).catch(failed);
exportMap.onclick = async () => {
  exportMap.disabled = true;
  cutoverStatus.textContent = "";
  try {
    const response = await fetch("/api/embed-map.csv", { headers: headers() });
    if (!response.ok) throw new Error(`Could not download the embed map (${response.status}).`);
    await saveEmbedMap(response);
    cutoverStatus.textContent = "Embed map downloaded.";
  } catch (error) {
    cutoverStatus.textContent = error instanceof Error ? error.message : "Could not download the embed map.";
  } finally {
    exportMap.disabled = false;
  }
};
if (key.value) {
  inventory.disabled = transfer.disabled = true;
  void poll().catch(failed);
}
