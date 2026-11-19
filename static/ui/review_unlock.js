const comment = document.querySelector("#reviewUnlockComment");
const hash = window.location.hash.slice(1);
if (comment && /^comment-[a-f0-9]{32}$/.test(hash))
    comment.value = hash;
export {};
