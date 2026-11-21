import { createSegment, paintRuler } from './timeline_scale_layout.js';
const ZOOMS = [1, 2, 4, 8, 16, 32];
function byId(id) {
    return document.getElementById(id);
}
export function mountTimelineScale(selectClip) {
    const viewport = byId('timelineScaleViewport');
    const content = byId('timelineScaleContent');
    const clips = byId('timelineScaleClips');
    const ruler = byId('timelineScaleRuler');
    const cursor = byId('timelineScaleCursor');
    const zoomIn = byId('timelineZoomIn');
    const zoomOut = byId('timelineZoomOut');
    const fit = byId('timelineFit');
    const zoomValue = byId('timelineZoomValue');
    let segments = [];
    let selectedIndex = -1;
    let totalMs = 0;
    let zoomIndex = 0;
    let contentWidth = 0;
    function clampScroll() {
        const max = Math.max(0, contentWidth - viewport.clientWidth);
        viewport.scrollLeft = Math.max(0, Math.min(viewport.scrollLeft, max));
    }
    function updateButtons() {
        const empty = segments.length === 0;
        zoomIn.disabled = empty || zoomIndex >= ZOOMS.length - 1;
        zoomOut.disabled = empty || zoomIndex <= 0;
        fit.disabled = empty || (zoomIndex === 0 && viewport.scrollLeft <= 0);
        zoomValue.textContent = zoomIndex === 0 ? 'Fit' : `${ZOOMS[zoomIndex]}x`;
    }
    function renderWidth() {
        contentWidth = Math.max(1, viewport.clientWidth * ZOOMS[zoomIndex]);
        content.style.width = `${contentWidth}px`;
        paintRuler(ruler, totalMs, contentWidth);
        updateButtons();
    }
    function syncSelection() {
        for (const child of clips.querySelectorAll('[data-clip-index]')) {
            child.setAttribute('aria-pressed', String(Number(child.dataset.clipIndex) === selectedIndex));
        }
    }
    function rebuild() {
        let elapsed = 0;
        clips.replaceChildren(...segments.map(segment => {
            const node = createSegment(segment, elapsed, totalMs, selectClip);
            elapsed += segment.duration;
            return node;
        }));
    }
    function applyZoom(next, resetScroll = false) {
        if (next < 0 || next >= ZOOMS.length)
            return;
        if (next === zoomIndex && !resetScroll)
            return;
        const oldWidth = contentWidth || viewport.clientWidth * ZOOMS[zoomIndex];
        const anchorViewport = viewport.clientWidth / 2;
        const cursorX = cursor.offsetLeft;
        let anchorX = anchorViewport;
        if (!cursor.hidden &&
            cursorX >= viewport.scrollLeft &&
            cursorX <= viewport.scrollLeft + viewport.clientWidth) {
            anchorX = cursorX - viewport.scrollLeft;
        }
        const contentAnchor = viewport.scrollLeft + anchorX;
        const ratio = oldWidth > 0 ? contentAnchor / oldWidth : 0;
        zoomIndex = next;
        renderWidth();
        if (resetScroll) {
            viewport.scrollLeft = 0;
        }
        else {
            viewport.scrollLeft = ratio * contentWidth - anchorX;
        }
        clampScroll();
        updateButtons();
    }
    zoomIn.addEventListener('click', () => applyZoom(zoomIndex + 1));
    zoomOut.addEventListener('click', () => applyZoom(zoomIndex - 1));
    fit.addEventListener('click', () => applyZoom(0, true));
    const resizeObserver = new ResizeObserver(() => {
        renderWidth();
        clampScroll();
    });
    resizeObserver.observe(viewport);
    renderWidth();
    return {
        show(nextSegments, selected) {
            const same = nextSegments.length === segments.length && nextSegments.every((segment, index) => {
                const previous = segments[index];
                return segment.index === previous.index && segment.title === previous.title && segment.duration === previous.duration;
            });
            selectedIndex = selected;
            if (!same) {
                segments = nextSegments;
                totalMs = segments.reduce((sum, segment) => sum + segment.duration, 0);
                rebuild();
                renderWidth();
                clampScroll();
            }
            syncSelection();
        },
        position(index, elapsedMs) {
            let offset = 0;
            for (const segment of segments) {
                if (segment.index === index && totalMs > 0) {
                    const elapsed = Math.min(Math.max(elapsedMs, 0), segment.duration);
                    cursor.style.left = `${((offset + elapsed) / totalMs) * 100}%`;
                    cursor.hidden = false;
                    return;
                }
                offset += segment.duration;
            }
            cursor.hidden = true;
        },
    };
}
