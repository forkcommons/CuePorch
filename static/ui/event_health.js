export function relaySummary({ encoder }, unavailable) {
    if (!encoder)
        return unavailable;
    const label = { browser: "Browser relay", srt: "SRT relay", rtmp: "Low-latency RTMP relay",
        dash: "MPEG-DASH relay", program: "Prerecorded relay" }[encoder.source];
    const measurements = [label];
    if (encoder.inputs) {
        const { primary, backup, selected } = encoder.inputs;
        measurements.push(`Primary ${primary ? "receiving" : "waiting"}`, `Backup ${backup ? "receiving" : "waiting"}`);
        if (selected)
            measurements.push(`On air ${selected === "primary" ? "Primary" : "Backup"}`);
    }
    const values = encoder.metrics;
    if (!values)
        measurements.push("Waiting for measurements");
    if (values?.fps !== undefined)
        measurements.push(`${values.fps.toFixed(1)} fps`);
    if (values?.bitrate !== undefined)
        measurements.push(`${Math.round(values.bitrate)} kbps`);
    if (values?.drop_frames !== undefined)
        measurements.push(`${values.drop_frames} dropped frames`);
    return measurements.join(" · ");
}
