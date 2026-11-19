export function renderCommentText(element: HTMLElement, text: string) {
  let position = 0;
  for (const match of text.matchAll(/\bhttps?:\/\/[^\s<>"']+/gi)) {
    const candidate = match[0].replace(/[.,;!?]+$/, "");
    let url: URL;
    try {
      url = new URL(candidate);
    } catch {
      continue;
    }
    if (url.username || url.password) continue;
    element.append(text.slice(position, match.index));
    const link = document.createElement("a");
    link.href = url.href;
    link.textContent = candidate;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    element.append(link);
    position = match.index + candidate.length;
  }
  element.append(text.slice(position));
}
