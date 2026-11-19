import { setupPresentation } from "./video_presentation.js";
import { setupRatings } from "./video_ratings.js";

interface Chapter { title: string; timecode: number; }
interface Chapters { title: string; version: string; revision: string; chapters: Chapter[]; }

const identity = document.querySelector<HTMLFormElement>("#identify")!;
const key = document.querySelector<HTMLInputElement>("#chapterKey")!;
const status = document.querySelector<HTMLElement>("#chapterStatus")!;
const editor = document.querySelector<HTMLElement>("#interactivityEditor")!;
const title = document.querySelector<HTMLElement>("#interactivityTitle")!;
const preview = document.querySelector<HTMLVideoElement>("#chapterPreview")!;
const form = document.querySelector<HTMLFormElement>("#chapterEditor")!;
const list = document.querySelector<HTMLOListElement>("#chapterRows")!;
const rowTemplate = document.querySelector<HTMLTemplateElement>("#chapterRow")!;
const add = document.querySelector<HTMLButtonElement>("#addChapter")!;
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
let state: Chapters;

key.value = sessionStorage.getItem("cueporchOperator") || "";

async function call<T>(suffix: string, method = "GET", body?: object): Promise<T> {
  const headers: Record<string, string> = {Authorization: "Bearer " + key.value};
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(endpoint + suffix, {method, headers, body: body && JSON.stringify(body)});
  if (!response.ok) {
    const message = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(message?.textContent || `Request failed (${response.status})`);
  }
  return response.json();
}

async function addPicture(file: File) {
  const body = new FormData();
  body.set("logo", file);
  const response = await fetch("/api/review-logos", {
    method: "POST", body, headers: {Authorization: "Bearer " + key.value},
  });
  if (!response.ok) {
    const message = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(message?.textContent || `Image upload failed (${response.status})`);
  }
  const result: {logo: string} = await response.json();
  return result.logo;
}

function timestamp(value: string) {
  const match = /^(?:(\d{1,6}):)?([0-5]?\d):([0-5]\d)$/.exec(value.trim());
  if (!match) throw new Error("Use start times such as 00:01:25.");
  return Number(match[1] || 0) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

function displayTime(seconds: number) {
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map((value) => String(value).padStart(2, "0")).join(":");
}

const showPresentation = setupPresentation(call, addPicture, timestamp, displayTime, preview);
const showRatings = setupRatings(call, timestamp, displayTime, async () => {
  const response = await fetch(endpoint + "/rating/export", {headers: {Authorization: "Bearer " + key.value}});
  if (!response.ok) throw new Error("The rating report could not be exported.");
  const uri = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = uri;
  link.download = "cueporch-video-rating.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(uri), 1000);
});

function appendChapter(chapter: Chapter) {
  const row = rowTemplate.content.firstElementChild!.cloneNode(true) as HTMLElement;
  const name = row.querySelector<HTMLInputElement>("[name=title]")!;
  const time = row.querySelector<HTMLInputElement>("[name=timecode]")!;
  name.value = chapter.title;
  time.value = displayTime(chapter.timecode);
  const watch = row.querySelector<HTMLButtonElement>("[data-chapter-action=preview]")!;
  watch.hidden = preview.hidden;
  watch.onclick = () => {
    try {
      preview.currentTime = timestamp(time.value);
      status.textContent = "";
    } catch (error) {
      status.textContent = String(error);
    }
  };
  row.querySelector<HTMLButtonElement>("[data-chapter-action=remove]")!.onclick = () => {
    row.remove();
    add.disabled = false;
  };
  list.append(row);
  add.disabled = list.children.length >= 80;
  return name;
}

function present(value: Chapters) {
  state = value;
  title.textContent = "Interactivity for " + value.title;
  list.replaceChildren();
  add.disabled = false;
  value.chapters.forEach(appendChapter);
}

async function show() {
  try {
    const settings = await call<Chapters>("/chapters");
    const work = await call<{preview: string | null}>("");
    if (work.preview) {
      const ticket = await call<{preview: string}>("/ticket", "POST", {sha256: work.preview});
      preview.src = ticket.preview;
      preview.hidden = false;
    }
    present(settings);
    sessionStorage.setItem("cueporchOperator", key.value);
    identity.hidden = true;
    editor.hidden = false;
    status.textContent = "";
    await Promise.all([showPresentation(), showRatings()]);
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

identity.onsubmit = (event) => { event.preventDefault(); void show(); };
if (key.value) void show();

add.onclick = () => appendChapter({title: "", timecode: Math.floor(preview.currentTime || 0)}).focus();
form.onsubmit = async (event) => {
  event.preventDefault();
  try {
    const chapters = [...list.children].map((row) => ({
      title: row.querySelector<HTMLInputElement>("[name=title]")!.value,
      timecode: timestamp(row.querySelector<HTMLInputElement>("[name=timecode]")!.value),
    }));
    present(await call<Chapters>("/chapters", "PUT", {version: state.version, revision: state.revision, chapters}));
    status.textContent = "Chapters saved.";
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
};
