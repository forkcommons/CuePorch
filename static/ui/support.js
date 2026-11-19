const report = document.querySelector("form#report");
const work = document.querySelector("input#work");
const details = document.querySelector("textarea#details");
const caseId = document.querySelector("input#caseId");
const key = document.querySelector("input#key");
const listCases = document.querySelector("button#listCases");
const caseList = document.querySelector("ul#caseList");
const readCase = document.querySelector("button#readCase");
const note = document.querySelector("textarea#note");
const kind = document.querySelector("select#kind");
const suspend = document.querySelector("input#suspend");
const reply = document.querySelector("button#reply");
const result = document.querySelector("pre#result");
const headers = () => ({
    Authorization: "Bearer " + key.value,
    "Content-Type": "application/json",
});
report.onsubmit = async (e) => {
    e.preventDefault();
    const r = await fetch("/api/works/" + encodeURIComponent(work.value) + "/complaints", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ details: details.value }),
    });
    if (!r.ok) {
        result.textContent = "Case creation failed";
        return;
    }
    const data = await r.json();
    caseId.value = data.case;
    key.value = data.case_key;
    result.textContent =
        "Save your case ID and private case key: " +
            data.case +
            " " +
            data.case_key;
};
const url = () => "/api/cases/" + encodeURIComponent(caseId.value);
readCase.onclick = async () => {
    const r = await fetch(url(), { headers: headers() });
    result.textContent = r.ok
        ? JSON.stringify(await r.json(), null, 2)
        : "Access denied";
};
reply.onclick = async () => {
    const r = await fetch(url(), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({
            kind: kind.value,
            details: note.value,
            suspend_screenings: suspend.checked,
        }),
    });
    if (r.ok)
        readCase.click();
    else
        result.textContent = "Response failed";
};
listCases.onclick = async () => {
    const r = await fetch("/api/cases", { headers: headers() });
    if (!r.ok) {
        result.textContent = "Operator access required";
        return;
    }
    caseList.replaceChildren();
    for (const c of await r.json()) {
        let li = document.createElement("li"), b = document.createElement("button");
        b.textContent = c.work + " · " + c.id;
        b.onclick = () => {
            caseId.value = c.id;
            readCase.click();
        };
        li.append(b);
        caseList.append(li);
    }
};
export {};
