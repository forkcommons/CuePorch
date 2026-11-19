export type Layout = "inset" | "side" | "screen";

export interface Composition {
  layout: Layout;
  color: string;
  background: "solid" | "gradient";
}

function fit(
  context: CanvasRenderingContext2D, source: HTMLVideoElement,
  left: number, top: number, width: number, height: number,
) {
  if (source.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !source.videoWidth) return;
  const scale = Math.min(width / source.videoWidth, height / source.videoHeight);
  const w = scale * source.videoWidth;
  const h = scale * source.videoHeight;
  context.drawImage(source, left + (width - w) / 2, top + (height - h) / 2, w, h);
}

export function drawRecording(
  context: CanvasRenderingContext2D, options: Composition,
  camera: HTMLVideoElement | null, screen: HTMLVideoElement | null,
) {
  const {width, height} = context.canvas;
  if (options.background === "gradient") {
    const gradient = context.createLinearGradient(0, 0, width, height);
    gradient.addColorStop(0, options.color);
    gradient.addColorStop(1, "#162528");
    context.fillStyle = gradient;
  } else {
    context.fillStyle = options.color;
  }
  context.fillRect(0, 0, width, height);
  if (screen && camera && options.layout === "side") {
    fit(context, screen, 0, 0, width / 2, height);
    fit(context, camera, width / 2, 0, width / 2, height);
    return;
  }
  const primary = screen || camera;
  if (primary) fit(context, primary, 0, 0, width, height);
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

export function recordingSize(shortSide: number, aspect: string) {
  const [horizontal, vertical] = aspect.split(":").map(Number);
  const factor = shortSide / Math.min(horizontal, vertical);
  return {width: 2 * Math.round(horizontal * factor / 2), height: 2 * Math.round(vertical * factor / 2)};
}

export function mediaPreview(source: MediaStream) {
  if (!source.getVideoTracks().length) return null;
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.srcObject = source;
  void video.play().catch(() => {});
  return video;
}

export function recordingMime() {
  return ["video/webm;codecs=vp8,opus", "video/webm", "video/mp4"].find(
    format => MediaRecorder.isTypeSupported(format),
  );
}
