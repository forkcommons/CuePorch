class MicrophonePcm extends AudioWorkletProcessor {
    data = new Int16Array(sampleRate / 2);
    offset = 0;
    process(inputs) {
        const samples = inputs[0]?.[0];
        if (!samples)
            return true;
        for (const sample of samples) {
            this.data[this.offset++] = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
            if (this.offset === this.data.length) {
                this.port.postMessage(this.data.buffer, [this.data.buffer]);
                this.data = new Int16Array(sampleRate / 2);
                this.offset = 0;
            }
        }
        // An unmodified output stays silent. The speaker hears no microphone loopback.
        return true;
    }
}
registerProcessor("cueporch-voice-pcm", MicrophonePcm);
export {};
