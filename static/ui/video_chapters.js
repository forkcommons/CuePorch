const menu = document.querySelector("[data-chapter-menu]");
const select = menu.querySelector("select");
const video = document.querySelector("video");
const chapters = [...select.options].slice(1).reverse();
select.addEventListener("change", () => {
    if (select.value)
        video.currentTime = Number(select.value);
});
function selectCurrent() {
    const current = chapters.find((option) => Number(option.value) <= video.currentTime);
    select.value = current?.value || "";
}
video.addEventListener("timeupdate", selectCurrent);
selectCurrent();
menu.hidden = false;
export {};
