"""Check that a private Library recording keeps audio already in the host replay."""

from array import array
from contextlib import nullcontext
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest

from event_library_recordings import Recordings


def make_fixture(path):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error",
         "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=1",
         "-map", "0:v", "-map", "1:a", "-map", "2:a",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1", "-c:a", "aac",
         "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=spa", str(path)],
        check=True, timeout=30,
    )


class LibraryRecordingAudioTest(unittest.TestCase):
    def test_retains_both_audio_streams(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.mp4"
            make_fixture(source)
            payload = source.read_bytes()
            response = SimpleNamespace(iter_content=lambda _: iter([payload]))
            recorder = SimpleNamespace(
                stopping=Event(),
                media=SimpleNamespace(read=lambda *args, **kwargs: nullcontext(response)),
            )
            Recordings.retain(recorder, {"origin": "https://peer.example"},
                              [("/remote.mp4", len(payload))], folder)
            retained = folder / "recording.mp4"
            for stream, expected in enumerate((440, 880)):
                decoded = subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-i", str(retained),
                     "-map", f"0:a:{stream}", "-t", "0.5", "-ac", "1", "-ar", "8000",
                     "-f", "s16le", "pipe:1"], capture_output=True, check=True, timeout=10,
                ).stdout
                self.assertGreaterEqual(len(decoded), 8000)
                samples = array("h")
                samples.frombytes(decoded)
                crossings = sum((left < 0) != (right < 0) for left, right in zip(samples, samples[1:]))
                self.assertAlmostEqual(crossings * 4000 / len(samples), expected, delta=20)
