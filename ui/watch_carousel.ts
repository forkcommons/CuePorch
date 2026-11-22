const carousel = document.querySelector<HTMLElement>("[data-watch-carousel]")!;
const slides = [...carousel.querySelectorAll<HTMLElement>("[data-watch-slide]")];
const navigation = carousel.querySelector<HTMLElement>("[data-watch-navigation]")!;
const counter = navigation.querySelector<HTMLOutputElement>("[data-watch-count]")!;
const countFormat = navigation.dataset.countFormat ?? "{current} of {total}";
let index = 0;

function show(selected: number) {
  slides[index].hidden = true;
  index = (selected + slides.length) % slides.length;
  slides[index].hidden = false;
  counter.value = countFormat.replace("{current}", String(index + 1)).replace("{total}", String(slides.length));
}

navigation.querySelector("[data-watch-previous]")!.addEventListener("click", () => show(index - 1));
navigation.querySelector("[data-watch-next]")!.addEventListener("click", () => show(index + 1));
show(0);
navigation.hidden = false;
