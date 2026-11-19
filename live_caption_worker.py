"""Read the CuePorch program's PCM and reuse TextFlowKit's streaming core."""

import json
import sys

from textflowkit.core.streaming import StreamingError, StreamingSession


PCM_FRAME_BYTES = 16000  # One half second of 16 kHz, 16-bit mono.
PCM_QUEUE_BYTES = 5 * 32000


def send(kind, **values):
    print(json.dumps(dict(type=kind, **values), separators=(",", ":")), flush=True)


def transcribe(language):
    live = StreamingSession(language=language)
    last = 0

    def emit(event):
        nonlocal last
        if event is not None and event.seq > last:
            last = event.seq
            send("cue", event=event.to_dict())

    try:
        live.start()
        send("ready", sample_rate=16000)
        while block := sys.stdin.buffer.read(PCM_FRAME_BYTES):
            while live.queued_audio_bytes + len(block) > PCM_QUEUE_BYTES:
                emit(live.read_event(timeout=0.2))
            live.feed(block)
            event = live.read_event(timeout=0.04)
            while event is not None:
                emit(event)
                event = live.read_event(timeout=0)
        terminal = live.finish(timeout=15)
        for event in live.events_since(last + 1):
            emit(event)
        send("finished", event=terminal.to_dict(), duration=live.duration_s)
        return 0
    except StreamingError as error:
        send("error", code=error.code)
        return 1
    finally:
        if not live.finished:
            live.cancel()


if __name__ == "__main__":
    sys.exit(transcribe(sys.argv[1] if len(sys.argv) == 2 else None))
