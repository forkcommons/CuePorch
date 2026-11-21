import { drawRecording, recordingMime, recordingSize, } from "./recording_composition.js";
function paintProductFrame(context, photo, title) {
    drawRecording(context, { layout: "screen", color: "#111827", background: "solid" }, null, photo);
    const { width, height } = context.canvas;
    const bandHeight = Math.max(28, Math.round(height * 0.09));
    context.fillStyle = "rgba(17, 24, 39, 0.85)";
    context.fillRect(0, height - bandHeight, width, bandHeight);
    const fontSize = Math.max(12, Math.round(bandHeight * 0.42));
    context.font = `600 ${fontSize}px system-ui, sans-serif`;
    context.fillStyle = "#f9fafb";
    context.textBaseline = "middle";
    const inset = Math.round(bandHeight * 0.35);
    context.fillText(title, inset, height - bandHeight / 2, width - inset * 2);
}
async function recordProductFrames(canvas, paint, count) {
    const mime = recordingMime();
    const stream = canvas.captureStream(30);
    try {
        const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        return await new Promise((resolve, reject) => {
            const chunks = [];
            let timer;
            const cleanup = () => {
                if (timer !== undefined) {
                    clearInterval(timer);
                    timer = undefined;
                }
            };
            recorder.ondataavailable = (e) => {
                if (e.data.size)
                    chunks.push(e.data);
            };
            recorder.onstop = () => {
                cleanup();
                resolve(new Blob(chunks, { type: recorder.mimeType }));
            };
            recorder.onerror = (e) => {
                cleanup();
                reject(e);
            };
            const start = performance.now();
            paint(0);
            recorder.start();
            timer = setInterval(() => {
                const i = Math.floor((performance.now() - start) / 1600);
                if (i >= count) {
                    cleanup();
                    recorder.stop();
                    return;
                }
                try {
                    paint(i);
                }
                catch (err) {
                    cleanup();
                    reject(err);
                    recorder.stop();
                }
            }, 100);
        });
    }
    finally {
        stream.getTracks().forEach((t) => t.stop());
    }
}
export async function createProductVideo(title, files) {
    const name = title.trim().slice(0, 100);
    if (!name)
        throw new Error("Title required");
    if (files.length < 1 || files.length > 8)
        throw new Error("1-8 files required");
    const urls = [], images = [];
    try {
        for (const file of files) {
            const url = URL.createObjectURL(file);
            urls.push(url);
            const img = new Image();
            img.src = url;
            await img.decode();
            images.push(img);
        }
        const size = recordingSize(360, "16:9");
        const canvas = document.createElement("canvas");
        canvas.width = size.width;
        canvas.height = size.height;
        const ctx = canvas.getContext("2d");
        if (!ctx)
            throw new Error("Canvas 2D context unavailable");
        const blob = await recordProductFrames(canvas, (i) => paintProductFrame(ctx, images[i], name), images.length);
        const ext = blob.type.startsWith("video/mp4") ? "mp4" : "webm";
        const safe = name.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_");
        return new File([blob], `${safe}.${ext}`, { type: blob.type });
    }
    finally {
        urls.forEach((u) => URL.revokeObjectURL(u));
    }
}
