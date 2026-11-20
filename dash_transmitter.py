"""Package an external encoder feed and retry complete MPEG-DASH uploads."""

import argparse
from ipaddress import ip_address
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urljoin, urlsplit

import requests

from event_dash_format import MEDIA_FILE, parse_manifest


class RetryUpload(Exception):
    pass


def fragment_order(name):
    segment = MEDIA_FILE.fullmatch(name)
    return (int(segment.group(2)) if segment else 0, name)


def upload_url(value):
    address = urlsplit(value)
    hostname = address.hostname
    try:
        local = hostname == "localhost" or ip_address(hostname or "").is_loopback
    except ValueError:
        local = False
    if (not hostname or address.username is not None or address.password is not None or
            address.query or address.fragment or not address.path.endswith("/feed.mpd") or
            (address.scheme != "https" and not (address.scheme == "http" and local))):
        raise ValueError("Use the HTTPS DASH manifest address from CuePorch production.")
    return value


class Uploader:
    def __init__(self, client, directory, manifest, token):
        self.client = client
        self.directory = directory
        self.manifest = upload_url(manifest)
        if not isinstance(token, str) or len(token) < 24 or not token.isascii():
            raise ValueError("Set CUEPORCH_DASH_TOKEN to this occurrence's upload token.")
        self.headers = {"Authorization": "Bearer " + token}
        self.sent = set()
        self.latest_manifest = None
        self.ended = False

    def put(self, filename, body):
        try:
            response = self.client.put(urljoin(self.manifest, filename), data=body, headers=self.headers,
                                       timeout=(3, 8), allow_redirects=False)
        except (requests.ConnectionError, requests.Timeout) as error:
            raise RetryUpload("CuePorch could not be reached.") from error
        with response:
            if response.status_code == 429 or response.status_code >= 500 and response.status_code != 507:
                raise RetryUpload(f"CuePorch returned HTTP {response.status_code}.")
            if response.status_code != 204:
                raise ValueError(f"CuePorch rejected {filename} with HTTP {response.status_code}.")

    def flush(self):
        manifest_path = self.directory / "feed.mpd"
        if not manifest_path.is_file():
            return False
        payload = manifest_path.read_bytes()
        if payload == self.latest_manifest:
            return self.ended
        tracks, ended = parse_manifest(payload)
        files = {track.init_file for track in tracks.values()} | {
            track.filename(fragment.sequence) for track in tracks.values() for fragment in track.segments
        }
        self.sent.intersection_update(files)
        for filename in sorted(files - self.sent, key=fragment_order):
            try:
                data = (self.directory / filename).read_bytes()
            except (FileNotFoundError, PermissionError):
                return False
            self.put(filename, data)
            self.sent.add(filename)
        self.put("feed.mpd", payload)
        self.latest_manifest = payload
        self.ended = ended
        return ended


def ffmpeg_arguments(binary, source, listen):
    receiver = ["-listen", "1", "-timeout", "300"] if listen else []
    return [binary, "-nostdin", "-loglevel", "warning", *receiver, "-i", source,
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-sc_threshold", "0", "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-f", "dash", "-seg_duration", "2",
            "-window_size", "90", "-extra_window_size", "30", "feed.mpd"]


def transmit(source, listen, manifest, token, ffmpeg, ca_file):
    with TemporaryDirectory(prefix="cueporch-dash-transmitter-") as temporary, requests.Session() as client:
        client.verify = str(ca_file) if ca_file else True
        directory = Path(temporary)
        uploader = Uploader(client, directory, manifest, token)
        command = ffmpeg_arguments(ffmpeg, source, listen)
        with subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as encoder:
            try:
                failures, ended_at = 0, None
                while True:
                    if encoder.poll() is not None and ended_at is None:
                        ended_at = monotonic()
                    try:
                        if uploader.flush() and ended_at is not None:
                            return encoder.returncode
                        failures = 0
                    except (RetryUpload, PermissionError):
                        failures = min(failures + 1, 4)
                    if ended_at is not None and monotonic() - ended_at > 25:
                        raise ValueError("The transmitter could not finish sending the final DASH manifest.")
                    sleep(min(2, .2 * 2 ** failures))
            finally:
                if encoder.poll() is None:
                    encoder.terminate()
                    try:
                        encoder.wait(5)
                    except subprocess.TimeoutExpired:
                        encoder.kill()
                        encoder.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="DASH manifest endpoint shown in CuePorch")
    parser.add_argument("--source", default="rtmp://127.0.0.1:11938/live/cueporch", help="FFmpeg input")
    parser.add_argument("--listen", action="store_true", help="Wait for an external encoder at the source RTMP address")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    parser.add_argument("--ca-file", type=Path, help="Trusted CA PEM for a private CuePorch origin")
    options = parser.parse_args(argv)
    try:
        return transmit(options.source, options.listen, options.manifest, os.environ.get("CUEPORCH_DASH_TOKEN"),
                        options.ffmpeg, options.ca_file)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
