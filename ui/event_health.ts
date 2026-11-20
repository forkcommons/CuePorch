export interface RelayHealth {
  encoder?: {
    source: "browser" | "srt" | "rtmp" | "dash" | "program";
    metrics: {fps?: number; bitrate?: number; drop_frames?: number} | null;
  } | null;
}

export function relaySummary({encoder}: RelayHealth, unavailable: string) {
  if (!encoder) return unavailable;
  const label = {browser: "Browser relay", srt: "SRT relay", rtmp: "Low-latency RTMP relay",
    dash: "MPEG-DASH relay", program: "Prerecorded relay"}[encoder.source];
  const values = encoder.metrics;
  if (!values) return `${label} · Waiting for measurements`;
  const measurements = [label];
  if (values.fps !== undefined) measurements.push(`${values.fps.toFixed(1)} fps`);
  if (values.bitrate !== undefined) measurements.push(`${Math.round(values.bitrate)} kbps`);
  if (values.drop_frames !== undefined) measurements.push(`${values.drop_frames} dropped frames`);
  return measurements.join(" · ");
}
