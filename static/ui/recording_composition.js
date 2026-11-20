function fit(context, source, left, top, width, height) {
    if (source instanceof HTMLVideoElement && source.readyState < HTMLMediaElement.HAVE_CURRENT_DATA)
        return;
    const [sourceWidth, sourceHeight] = source instanceof HTMLVideoElement
        ? [source.videoWidth, source.videoHeight] : [source.naturalWidth, source.naturalHeight];
    if (!sourceWidth || !sourceHeight)
        return;
    const scale = Math.min(width / sourceWidth, height / sourceHeight);
    const w = scale * sourceWidth;
    const h = scale * sourceHeight;
    context.drawImage(source, left + (width - w) / 2, top + (height - h) / 2, w, h);
}
export function drawStudioLogo(context, logo) {
    const { width, height } = context.canvas;
    fit(context, logo, width * .85, height * .82, width * .12, height * .14);
}
export function drawRecording(context, options, camera, screen) {
    const { width, height } = context.canvas;
    if (options.background === "gradient") {
        const gradient = context.createLinearGradient(0, 0, width, height);
        gradient.addColorStop(0, options.color);
        gradient.addColorStop(1, "#162528");
        context.fillStyle = gradient;
    }
    else {
        context.fillStyle = options.color;
    }
    context.fillRect(0, 0, width, height);
    if (screen && camera && options.layout === "side") {
        fit(context, screen, 0, 0, width / 2, height);
        fit(context, camera, width / 2, 0, width / 2, height);
        return;
    }
    const primary = screen || camera;
    if (primary)
        fit(context, primary, 0, 0, width, height);
    if (screen && camera && options.layout === "inset") {
        const insetWidth = width * .28;
        const insetHeight = height * .29;
        const left = width - insetWidth - width * .025;
        const top = height - insetHeight - height * .04;
        context.fillStyle = options.color;
        context.fillRect(left, top, insetWidth, insetHeight);
        fit(context, camera, left, top, insetWidth, insetHeight);
    }
}
export function recordingSize(shortSide, aspect) {
    const [horizontal, vertical] = aspect.split(":").map(Number);
    const factor = shortSide / Math.min(horizontal, vertical);
    return { width: 2 * Math.round(horizontal * factor / 2), height: 2 * Math.round(vertical * factor / 2) };
}
export function drawTileGrid(context, sources, color) {
    const { width, height } = context.canvas;
    const columns = Math.ceil(Math.sqrt(sources.length * width / height));
    const rows = Math.ceil(sources.length / columns);
    const tileWidth = width / columns, tileHeight = height / rows;
    context.fillStyle = color;
    context.fillRect(0, 0, width, height);
    for (const [index, source] of sources.entries()) {
        fit(context, source, index % columns * tileWidth, Math.floor(index / columns) * tileHeight, tileWidth, tileHeight);
    }
}
export function mediaPreview(source) {
    if (!source.getVideoTracks().length)
        return null;
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = source;
    void video.play().catch(() => { });
    return video;
}
export function recordingMime() {
    return ["video/webm;codecs=vp8,opus", "video/webm", "video/mp4"].find(format => MediaRecorder.isTypeSupported(format));
}
