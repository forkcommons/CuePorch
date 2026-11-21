"""Decode occurrence output through a local RTMPS receiver.

Place the bundled FFmpeg on PATH so the encoder can use the test CA file.
Windows FFmpeg builds using Schannel may use the Windows trust store instead.
"""

import base64
import contextlib
import hmac
import io
import os
import shutil
import sqlite3
import subprocess
import threading
import types
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

import event_simulcast
import installed_simulcast as sim


def check_youtube_rtmps(root, database, destination_id, operator):
    scratch = root / "youtube-egress"
    scratch.mkdir()

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg missing"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", "12", "-g", "24",
        "-hls_time", "1", "-hls_list_size", "0",
        "-hls_segment_filename", str(scratch / "320-%d.ts"),
        str(scratch / "320.m3u8"),
    ], check=True, creationflags=flags,
       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (scratch / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x180\n320.m3u8\n"
    )

    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM live_simulcast_destination WHERE id=?",
            (destination_id,),
        ).fetchone()
    assert row is not None

    cipher = Fernet(base64.urlsafe_b64encode(
        hmac.digest(operator.encode(), b"CuePorch RTMP streams", "sha256")
    ))
    cert, tls_key = sim.tls_certificate(scratch)
    port = sim.free_port()
    flv = scratch / "out.flv"
    origin = "https://origin.local"
    remote = "remote1"
    master_url = f"{origin}/static/streaming-playlists/hls/{remote}/master.m3u8"

    class FakeMedia:
        def metadata(self, media_origin, media_remote):
            return {
                "privacy": {"id": 1},
                "state": {"id": 1},
                "isLive": True,
                "streamingPlaylists": [
                    {"type": 1, "playlistUrl": master_url}
                ],
            }

        @contextlib.contextmanager
        def read(self, media_origin, path, stream=False):
            base = Path(path).name
            expected = f"/static/streaming-playlists/hls/{remote}/{base}"
            assert path == expected, (path, expected)
            file = scratch / base
            assert file.exists(), file
            yield types.SimpleNamespace(raw=io.BytesIO(file.read_bytes()))

    program = event_simulcast.Program(FakeMedia(), origin, remote, cipher, None)
    program.configure([row])
    lease = scratch / "lease"

    with sim.receive(ffmpeg, port, "fixture-live-stream-key", flv) as recv:
        with sim.tls_bridge(port, (cert, tls_key)) as (tls_port, handshake):
            original_init = event_simulcast.Leg.__init__

            def patched_init(self, server, key, audio_required):
                assert server == "rtmps://a.rtmps.youtube.com:443/live2"
                assert key == "fixture-live-stream-key"
                loopback_url = f"rtmps://127.0.0.1:{tls_port}/live"
                return original_init(self, loopback_url, key, audio_required)

            with mock.patch.object(event_simulcast.Leg, "__init__", patched_init):
                with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                    t = threading.Thread(
                        target=program.run, args=(lease,), daemon=True
                    )
                    t.start()
                    try:
                        sim.wait_until(
                            lambda: sim.size(flv) > 12000 and handshake.is_set(),
                            30,
                            lambda: f"RTMPS delivered {sim.size(flv)} bytes, TLS handshake: {handshake.is_set()}",
                        )
                    finally:
                        program.stopping.set()
                        t.join(10)
                    assert not t.is_alive(), "Program thread did not terminate"

        recv.wait(timeout=8)

    sim.decode(ffmpeg, flv)
    print("Selected YouTube destination delivered decodable audio and video over verified local TLS.")
