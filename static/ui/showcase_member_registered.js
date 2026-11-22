if (window.parent !== window) {
    window.parent.postMessage("cueporch:member-registered", window.location.origin);
}
export {};
