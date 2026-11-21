function label(text, control) {
    const node = document.createElement("label");
    node.append(text, control);
    return node;
}
export class StudioAudio {
    list;
    empty;
    context = new AudioContext();
    output = this.context.createMediaStreamDestination();
    channels = new Map();
    controls = new Map();
    clipNodes = new WeakMap();
    samples = new Float32Array(256);
    constructor(list, empty) {
        this.list = list;
        this.empty = empty;
        // Supply audio frames while the stage has only a title card or other silent source.
        const silence = this.context.createConstantSource();
        silence.offset.value = 0;
        silence.connect(this.output);
        silence.start();
    }
    get hasProgramInput() {
        return Array.from(this.channels.values()).some(channel => channel.onAir);
    }
    updateGain(channel) {
        channel.gain.gain.value = channel.onAir && !channel.mute.checked ? Number(channel.fader.value) / 100 : 0;
        channel.state.textContent = !channel.onAir ? "Off air" : channel.mute.checked ? "Muted" : "In program mix";
    }
    add(input) {
        const row = document.createElement("fieldset");
        const legend = document.createElement("legend");
        legend.textContent = input.name;
        const state = document.createElement("p");
        const level = document.createElement("meter");
        level.max = 1;
        const fader = document.createElement("input");
        fader.type = "range";
        fader.min = "0";
        fader.max = "100";
        fader.value = this.controls.get(input.id)?.volume ?? "100";
        const mute = document.createElement("input");
        mute.type = "checkbox";
        mute.checked = this.controls.get(input.id)?.muted ?? false;
        const meter = this.context.createAnalyser();
        meter.fftSize = this.samples.length;
        const gain = this.context.createGain();
        gain.gain.value = 0;
        meter.connect(gain);
        gain.connect(this.output);
        const channel = { row, state, level, fader, mute, meter, gain, nodes: [], onAir: false };
        const update = () => {
            this.controls.set(input.id, { volume: fader.value, muted: mute.checked });
            this.updateGain(channel);
        };
        fader.oninput = update;
        mute.onchange = update;
        row.append(legend, state, label("Input level", level), label("Program volume", fader), label("Mute in program", mute));
        this.list.append(row);
        this.channels.set(input.id, channel);
        return channel;
    }
    connect(channel, media) {
        const previous = channel.media;
        if (previous === media || (Array.isArray(previous) && Array.isArray(media) &&
            previous.length === media.length && previous.every((track, index) => track === media[index])))
            return;
        channel.nodes.forEach(node => node.disconnect(channel.meter));
        if (media instanceof HTMLVideoElement) {
            let node = this.clipNodes.get(media);
            if (!node) {
                node = this.context.createMediaElementSource(media);
                this.clipNodes.set(media, node);
            }
            media.muted = false;
            channel.nodes = [node];
        }
        else {
            channel.nodes = media.map(track => this.context.createMediaStreamSource(new MediaStream([track])));
        }
        channel.nodes.forEach(node => node.connect(channel.meter));
        channel.media = media;
    }
    reconcile(inputs) {
        const current = new Set(inputs.map(input => input.id));
        for (const [id, channel] of this.channels) {
            if (current.has(id))
                continue;
            channel.nodes.forEach(node => node.disconnect(channel.meter));
            channel.meter.disconnect();
            channel.gain.disconnect();
            channel.row.remove();
            this.channels.delete(id);
        }
        for (const input of inputs) {
            const channel = this.channels.get(input.id) ?? this.add(input);
            channel.onAir = input.onAir;
            this.updateGain(channel);
            this.connect(channel, input.media);
        }
        this.empty.hidden = this.channels.size > 0;
    }
    updateMeters() {
        if (this.list.closest("details")?.open === false)
            return;
        for (const channel of this.channels.values()) {
            channel.meter.getFloatTimeDomainData(this.samples);
            const energy = this.samples.reduce((sum, sample) => sum + sample ** 2, 0);
            channel.level.value = Math.min(1, 3 * Math.sqrt(energy / this.samples.length));
        }
    }
}
