const submission = document.querySelector<HTMLFormElement>("form#submission")!;
const access = document.querySelector<HTMLInputElement>("input#access")!;
const master = document.querySelector<HTMLInputElement>("input#master")!;
const preview = document.querySelector<HTMLVideoElement>("video#preview")!;
const releaseTime =
  document.querySelector<HTMLInputElement>("input#releaseTime")!;
const message = document.querySelector<HTMLElement>("p#message")!;
let objectUrl: string | undefined;
master.onchange = () => {
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  if (master.files?.length) {
    objectUrl = URL.createObjectURL(master.files[0]);
    preview.src = objectUrl;
    preview.hidden = false;
  }
};
submission.onsubmit = async (e) => {
  e.preventDefault();
  const data = new FormData(submission);
  data.set(
    "scheduled",
    releaseTime.value
      ? String(new Date(releaseTime.value).getTime() / 1000)
      : "0",
  );
  const r = await fetch("/api/submissions", {
    method: "POST",
    headers: { Authorization: "Bearer " + access.value },
    body: data,
  });
  message.textContent = r.ok
    ? "Received " + (await r.json()).work
    : "Submission failed (" + r.status + ")";
};
