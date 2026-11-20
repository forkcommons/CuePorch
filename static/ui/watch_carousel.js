const carousel = document.querySelector("[data-watch-carousel]");
const slides = [...carousel.querySelectorAll("[data-watch-slide]")];
const navigation = carousel.querySelector("[data-watch-navigation]");
const counter = navigation.querySelector("[data-watch-count]");
let index = 0;
function show(selected) {
    slides[index].hidden = true;
    index = (selected + slides.length) % slides.length;
    slides[index].hidden = false;
    counter.value = `${index + 1} of ${slides.length}`;
}
navigation.querySelector("[data-watch-previous]").addEventListener("click", () => show(index - 1));
navigation.querySelector("[data-watch-next]").addEventListener("click", () => show(index + 1));
show(0);
navigation.hidden = false;
export {};
