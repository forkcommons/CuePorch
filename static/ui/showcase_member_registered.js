if (window.parent !== window) {
    const releaseEl = document.getElementById("showcaseMemberRelease");
    const entry = releaseEl ? JSON.parse((releaseEl?.textContent || "")) : null;
    window.parent.postMessage(entry
        ? { type: "cueporch:member-registered", entry }
        : "cueporch:member-registered", window.location.origin);
}
export {};
