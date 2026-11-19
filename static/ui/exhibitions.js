const key = document.querySelector("input#key");
key.value = sessionStorage.getItem("cueporchOperator") || "";
const load = document.querySelector("button#load");
const grant = document.querySelector("form#grant");
const cut = document.querySelector("select#cut");
const originField = document.querySelector("input#originField");
const audience = document.querySelector("input#audience");
const agreement = document.querySelector("input#agreement");
const starts = document.querySelector("input#starts");
const ends = document.querySelector("input#ends");
const zone = document.querySelector("span#zone");
const listGrants = document.querySelector("button#listGrants");
const grantList = document.querySelector("ul#grantList");
const result = document.querySelector("output#result");
const headers = () => ({
    Authorization: "Bearer " + key.value,
    "Content-Type": "application/json",
});
zone.textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
load.onclick = async () => {
    let r = await fetch("/api/screening-copies", { headers: headers() });
    if (!r.ok) {
        result.textContent = "Access denied";
        return;
    }
    cut.replaceChildren();
    for (const w of await r.json()) {
        const o = document.createElement("option");
        o.textContent = w.title + " (" + w.version.slice(0, 10) + ")";
        o.value = JSON.stringify(w);
        cut.append(o);
    }
};
grant.onsubmit = async (e) => {
    e.preventDefault();
    const w = JSON.parse(cut.value);
    const r = await fetch("/api/grants", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({
            work: w.work,
            version: w.version,
            origin: originField.value,
            audience: audience.value,
            agreement: agreement.value,
            starts: new Date(starts.value).getTime() / 1000,
            ends: new Date(ends.value).getTime() / 1000,
        }),
    });
    result.textContent = r.ok
        ? JSON.stringify(await r.json())
        : "Grant failed (" + r.status + ")";
};
listGrants.onclick = async () => {
    const r = await fetch("/api/grants", { headers: headers() });
    if (!r.ok)
        return;
    grantList.replaceChildren();
    for (const g of await r.json()) {
        let li = document.createElement("li");
        const now = Date.now() / 1000, state = g.revoked
            ? "Revoked"
            : g.suspended
                ? "Suspended"
                : g.status !== "eligible"
                    ? g.status
                    : now < g.starts
                        ? "Scheduled"
                        : now >= g.ends
                            ? "Expired"
                            : "Active";
        li.textContent =
            g.origin +
                " · " +
                g.audience +
                " · " +
                g.agreement +
                " · " +
                g.work +
                " / " +
                g.version.slice(0, 10) +
                " · " +
                new Date(g.starts * 1000).toLocaleString() +
                " – " +
                new Date(g.ends * 1000).toLocaleString() +
                " · " +
                state +
                " ";
        if (!g.revoked) {
            let btn = document.createElement("button");
            btn.textContent = "Revoke";
            btn.onclick = async () => {
                let res = await fetch("/api/grants/" + g.id, {
                    method: "DELETE",
                    headers: headers(),
                });
                if (res.ok)
                    listGrants.click();
            };
            li.append(btn);
        }
        grantList.append(li);
    }
};
export {};
