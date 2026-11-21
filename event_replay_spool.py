"""Retain closed TS segments for one private interpreted event replay."""

from dataclasses import asdict
from contextlib import suppress
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

from filelock import FileLock, Timeout

from event_audio import Fragment
from event_interpreter_timing import (
    aligned_pcm, audio_master, decode_pcm, encode_audio_segment, first_video_pts, number, read_timeline,
)


FREE_RESERVE = 512 * 1024**2
CAPTURE_LIMIT = 8 * 1024**3
ARCHIVE_MANIFESTS = ("master.m3u8", "1.m3u8", "2.m3u8")
SEGMENT = re.compile(r"[01]-\d{9}\.ts")


def key(uid, opens, remote):
    return hashlib.sha256(json.dumps([uid, opens, remote]).encode("utf-8")).hexdigest()


def atomic_text(path, content):
    temp = path.with_suffix(".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def cleanup_flat(directory):
    for path in directory.iterdir():
        if SEGMENT.fullmatch(path.name) or path.name in (
            *ARCHIVE_MANIFESTS, "capture.json", "ready.json", "capture.tmp", "ready.tmp",
            "master.tmp", "1.tmp", "2.tmp",
        ) or SEGMENT.fullmatch(path.stem + ".ts") and path.suffix == ".tmp":
            path.unlink(missing_ok=True)
    directory.rmdir()


def available(root, uid, opens, remote):
    directory = root / key(uid, opens, remote)
    try:
        with (directory / "ready.json").open("rb") as stream:
            contents = stream.read(2049)
        if len(contents) > 2048:
            return None
        data = json.loads(contents)
        identity, duration = data["identity"], float(data["duration"])
        if (re.fullmatch(r"[a-f0-9]{32}", identity) and math.isfinite(duration) and duration > 0
                and (directory / "master.m3u8").is_file()):
            return SimpleNamespace(identity=identity, directory=directory, duration=duration)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


class ProgramSpool:
    def __init__(self, directory, byte_limit, scope, lease):
        self.directory = directory
        self.byte_limit = byte_limit
        self.scope = scope
        self.lease = lease
        self.lock = Lock()
        self.identity = uuid4().hex
        self.fragments = []
        self.latest = -1
        self.size = 0
        self.failed = self.sealed = False
        self.language = self.name = None

    def keep(self, source):
        original = source.stat(follow_symlinks=False)
        if not (SEGMENT.fullmatch(source.name) and stat.S_ISREG(original.st_mode) and original.st_size):
            raise ValueError("Invalid archive segment.")
        if self.size + original.st_size > self.byte_limit or shutil.disk_usage(self.directory).free < FREE_RESERVE:
            raise OSError("The local event recording reached its storage budget.")
        target = self.directory / source.name
        os.link(source, target, follow_symlinks=False)
        linked = target.stat(follow_symlinks=False)
        if not os.path.samestat(original, linked):
            target.unlink()
            raise ValueError("The program segment changed during capture.")
        self.size += linked.st_size

    def collect(self, primary):
        with self.lock:
            if self.failed or self.sealed:
                return
            try:
                _, fragments = read_timeline(primary, 0)
                for fragment in fragments:
                    index = number(fragment)
                    if index <= self.latest:
                        continue
                    if index != self.latest + 1:
                        raise ValueError("The rolling program lost an archive segment.")
                    self.keep(primary / fragment.name)
                    self.fragments.append(fragment)
                    self.latest = index
            except FileNotFoundError:
                if (primary / "0.m3u8").is_file():
                    self.failed = True
            except (OSError, ValueError):
                self.failed = True

    def audio(self, source, item, has_interpretation):
        with self.lock:
            if self.failed or self.sealed or (self.language is None and not has_interpretation):
                return
            if self.language is not None and self.language != item.language:
                return
            try:
                self.keep(source)
                if self.language is None:
                    self.language, self.name = item.language, item.name
            except FileExistsError:
                pass
            except (OSError, ValueError):
                self.failed = True

    def finish(self):
        with self.lock:
            if self.sealed:
                return
            self.sealed = True
            try:
                if self.failed or not self.fragments or self.language is None:
                    cleanup_flat(self.directory)
                else:
                    capture = {
                        "scope": self.scope, "identity": self.identity,
                        "programme": [asdict(fragment) for fragment in self.fragments],
                        "name": self.name, "language": self.language, "limit": self.byte_limit,
                    }
                    atomic_text(self.directory / "capture.json", json.dumps(capture))
            finally:
                self.lease.release()


def reserve(root, uid, opens, remote):
    scope = uid, opens, remote
    directory = root / key(*scope)
    lease = FileLock(str(directory.with_suffix(".lock")), timeout=0, thread_local=False)
    try:
        root.mkdir(exist_ok=True)
        lease.acquire()
        try:
            directory.mkdir()
        except OSError:
            lease.release()
            raise
    except (OSError, Timeout):
        return None
    return ProgramSpool(directory, CAPTURE_LIMIT, scope, lease)


def replay_manifests(programme, name, language):
    if (not programme
            or any(number(fragment) != index or fragment.initialization
                   or not SEGMENT.fullmatch(fragment.name) or not fragment.name.startswith("0-")
                   or not 0 < fragment.duration <= 30
                   for index, fragment in enumerate(programme))):
        raise ValueError("Invalid replay timeline.")
    target = math.ceil(max(fragment.duration for fragment in programme))
    headers = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
               "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    video, audio = headers.copy(), headers.copy()
    for fragment in programme:
        duration = f"#EXTINF:{fragment.duration:.6f},"
        video.extend((duration, fragment.name))
        audio.extend((duration, "1-" + fragment.name[2:]))
    video.append("#EXT-X-ENDLIST")
    audio.append("#EXT-X-ENDLIST")
    return {"2.m3u8": "\n".join(video) + "\n", "1.m3u8": "\n".join(audio) + "\n",
            "master.m3u8": audio_master(language, name)}


def finish_archive(directory, payload, stopping):
    programme = [Fragment(**item) for item in payload["programme"]]
    manifests = replay_manifests(programme, payload["name"], payload["language"])
    size = sum(path.stat().st_size for path in directory.iterdir() if SEGMENT.fullmatch(path.name))
    for fragment in programme:
        if stopping.is_set():
            raise InterruptedError
        base = directory / fragment.name
        if not base.is_file():
            raise ValueError("An archived program segment is missing.")
        alternate = directory / ("1-" + base.name[2:])
        if alternate.is_file():
            continue
        if shutil.disk_usage(directory).free < FREE_RESERVE or size >= payload["limit"]:
            raise OSError("The local event recording reached its storage budget.")
        try:
            fallback = decode_pcm(base)
        except subprocess.CalledProcessError:
            fallback = b""
        pcm = aligned_pcm(fragment, (), lambda source: b"", fallback)
        encode_audio_segment(pcm, first_video_pts(base), alternate)
        size += alternate.stat().st_size
    if size > payload["limit"]:
        raise OSError("The local event recording reached its storage budget.")
    for name, manifest in manifests.items():
        atomic_text(directory / name, manifest)
    atomic_text(directory / "ready.json", json.dumps({
        "identity": payload["identity"], "duration": sum(fragment.duration for fragment in programme),
    }))
    with suppress(OSError):
        (directory / "capture.json").unlink(missing_ok=True)


def finalize_next(root, stopping, permitted):
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if (not re.fullmatch(r"[a-f0-9]{64}", directory.name) or directory.is_symlink()
                or not directory.is_dir() or (directory / "ready.json").exists()):
            continue
        try:
            with FileLock(str(directory.with_suffix(".lock")), timeout=0):
                if not directory.is_dir() or (directory / "ready.json").exists():
                    continue
                capture = directory / "capture.json"
                try:
                    if capture.is_file():
                        payload = json.loads(capture.read_text(encoding="utf-8"))
                        scope = payload["scope"]
                        if key(*scope) == directory.name and permitted(*scope):
                            finish_archive(directory, payload, stopping)
                            return
                    cleanup_flat(directory)
                except InterruptedError:
                    return
                except (OSError, ValueError, LookupError, TypeError, StopIteration, subprocess.SubprocessError):
                    cleanup_flat(directory)
        except (OSError, Timeout):
            continue
