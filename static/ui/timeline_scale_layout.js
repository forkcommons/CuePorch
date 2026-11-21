function nice(ms) {
    const mag = 10 ** Math.floor(Math.log10(ms));
    const n = ms / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
}
function fmt(ms) {
    const s = ms / 1000;
    if (s >= 60)
        return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
    return `${Number(s.toFixed(3))}s`;
}
export function createSegment(segment, left, duration, select) {
    const el = document.createElement(segment.index === null ? 'div' : 'button');
    el.className = "timeline-scale-segment";
    const index = segment.index;
    if (index !== null) {
        el.type = 'button';
        el.dataset.clipIndex = String(index);
        el.addEventListener('click', () => select(index));
    }
    else {
        el.dataset.bookend = 'true';
    }
    el.style.left = `${duration > 0 ? (left / duration) * 100 : 0}%`;
    el.style.width = `${duration > 0 ? (segment.duration / duration) * 100 : 0}%`;
    const label = document.createElement("span");
    label.textContent = segment.title;
    el.append(label);
    const description = `${segment.title}, ${fmt(segment.duration)}, starts at ${fmt(left)}`;
    if (index !== null)
        el.setAttribute("aria-label", description);
    el.title = description;
    return el;
}
export function paintRuler(ruler, total, width) {
    ruler.textContent = '';
    if (total <= 0 || width <= 0)
        return;
    const step = nice(Math.max((80 * total) / width, total / 256, 1));
    const nodes = [0];
    for (let t = step; t < total - 1e-9 && nodes.length < 255; t += step)
        nodes.push(t);
    const last = nodes[nodes.length - 1];
    if (total > last && nodes.length < 256 && ((total - last) / total) * width >= 68)
        nodes.push(total);
    for (const t of nodes) {
        const tick = document.createElement('span');
        if (t === total)
            tick.dataset.end = "true";
        tick.style.left = `${(t / total) * 100}%`;
        tick.textContent = fmt(t);
        ruler.append(tick);
    }
}
