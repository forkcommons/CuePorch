const submission = document.querySelector("form#submission");
const access = document.querySelector("input#access");
const master = document.querySelector("input#master");
const preview = document.querySelector("video#preview");
const releaseTime = document.querySelector("input#releaseTime");
const message = document.querySelector("p#message");
let objectUrl;
master.onchange = () => {
    if (objectUrl)
        URL.revokeObjectURL(objectUrl);
    if (master.files?.length) {
        objectUrl = URL.createObjectURL(master.files[0]);
        preview.src = objectUrl;
        preview.hidden = false;
    }
};
submission.onsubmit = async (e) => {
    e.preventDefault();
    const data = new FormData(submission);
    data.set("scheduled", releaseTime.value
        ? String(new Date(releaseTime.value).getTime() / 1000)
        : "0");
    const r = await fetch("/api/submissions", {
        method: "POST",
        headers: { Authorization: "Bearer " + access.value },
        body: data,
    });
    message.textContent = r.ok
        ? "Received " + (await r.json()).work
        : "Submission failed (" + r.status + ")";
};
export {};
