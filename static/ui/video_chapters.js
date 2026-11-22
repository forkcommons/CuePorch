const menu = document.querySelector("[data-chapter-menu]");
const select = menu.querySelector("select");
const video = document.querySelector("video");
const picture = document.querySelector("[data-chapter-picture]");
const chapters = [...select.options].slice(1).reverse();
let currentSrc = "";
function updatePicture() {
    if (!picture)
        return;
    const src = select.selectedOptions[0]?.dataset.picture || "";
    if (src === currentSrc)
        return;
    currentSrc = src;
    picture.hidden = true;
    if (src) {
        picture.src = src;
    }
    else {
        picture.removeAttribute("src");
    }
}
picture?.addEventListener("load", () => {
    if (picture.getAttribute("src") === currentSrc)
        picture.hidden = false;
});
picture?.addEventListener("error", () => {
    if (picture.getAttribute("src") === currentSrc)
        picture.hidden = true;
});
select.addEventListener("change", () => {
    if (select.value)
        video.currentTime = Number(select.value);
    updatePicture();
});
function selectCurrent() {
    const current = chapters.find((option) => Number(option.value) <= video.currentTime);
    select.value = current?.value || "";
    updatePicture();
}
video.addEventListener("timeupdate", selectCurrent);
selectCurrent();
menu.hidden = false;
export {};
