"""Editor exports file source."""

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from flask import abort

from library_upload import VIDEO_INPUT, max_upload_bytes

POLL_SECONDS = 10
SCAN_LIMIT = 2000
SETTLE_NS = 20_000_000_000


def _is_video(name):
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix in VIDEO_INPUT


def _has_reparse(file_stat):
    attrs = getattr(file_stat, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def file_info_valid(file_stat):
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if file_stat.st_nlink != 1:
        return False
    if _has_reparse(file_stat):
        return False
    if file_stat.st_size <= 0 or file_stat.st_size > max_upload_bytes():
        return False
    return True


class FileSource:
    config = SimpleNamespace(key="editor-exports", name="Editor exports")

    def __init__(self, path):
        raw = (path or "").strip()
        self._root = self._configured_root(raw)
        normalized = os.path.normcase(os.path.abspath(raw)) if raw else None
        self.revision = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if normalized
            else None
        )

    @staticmethod
    def _configured_root(raw):
        if not raw or "\x00" in raw or not os.path.isabs(raw):
            return None
        try:
            cand = Path(os.path.normpath(raw))
            is_root = cand == Path(cand.anchor)
        except (ValueError, OSError):
            return None
        if is_root:
            return None
        return cand

    @property
    def root(self):
        return self._root

    @property
    def configured(self):
        return self._root is not None

    def authorize(self, con, revision):
        del con
        if not self.configured or not revision or revision != self.revision:
            abort(409, "editor exports revision mismatch")

    @staticmethod
    def identity(file_stat):
        return [
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
        ]

    def checked_root(self):
        root = self.root
        if root is None:
            abort(503, "editor exports source unavailable")
        try:
            info = os.lstat(root)
            if not stat.S_ISDIR(info.st_mode) or _has_reparse(info):
                raise OSError
        except OSError:
            abort(503, "editor exports source unavailable")
        return root

    def scan(self):
        root = self.checked_root()
        try:
            iterator = os.scandir(root)
        except OSError:
            abort(503, "editor exports source unavailable")

        results = {}
        with iterator:
            for count, entry in enumerate(iterator):
                if count >= SCAN_LIMIT:
                    abort(422, "editor exports source too large")
                item = self._consider(entry)
                if item is not None:
                    results[item[0]] = item[1]
        return results

    @staticmethod
    def _consider(entry):
        name = entry.name
        if not name or name.startswith(".") or not _is_video(name):
            return None
        try:
            # Windows DirEntry.stat omits file identity and hard-link count
            file_stat = os.lstat(entry.path)
        except OSError:
            return None
        if not file_info_valid(file_stat):
            return None
        return name, FileSource.identity(file_stat)


def baseline(source):
    state = {}
    for name, signature in source.scan().items():
        state[name] = {"stat": signature, "queued": True}
    return json.dumps(state)


def changes(source, watch):
    source.authorize(None, watch["connection"])
    prior = json.loads(watch["cursor"])
    now = time.time_ns()
    fresh = source.scan()
    cursor = {}
    entries = []
    for name, signature in fresh.items():
        previous = prior.get(name)
        if previous is not None and previous.get("stat") == signature:
            queued = bool(previous.get("queued"))
            if not queued and signature[3] + SETTLE_NS <= now:
                queued = True
                rev = hashlib.sha256(json.dumps(signature).encode("utf-8")).hexdigest()
                entries.append(
                    {"id": name, "name": name, "rev": rev, "stat": signature}
                )
        else:
            queued = False
        cursor[name] = {"stat": signature, "queued": queued}
    return {
        "entries": entries,
        "cursor": json.dumps(cursor),
        "has_more": False,
    }


def _export_name(name):
    if not isinstance(name, str) or not name or "\x00" in name:
        abort(400, "The export name is invalid.")
    if name in (".", "..") or name.startswith("."):
        abort(400, "The export name is invalid.")
    if "/" in name or "\\" in name or ":" in name or name != Path(name).name:
        abort(400, "The export name is invalid.")
    if not _is_video(name):
        abort(400, "The export name is invalid.")
    return name


def _export_matches(st, expected):
    return file_info_valid(st) and FileSource.identity(st) == expected


def save_export(source, entry, target):
    name = _export_name(entry["name"])
    path = source.checked_root() / name

    try:
        before = path.lstat()
        if not _export_matches(before, entry["stat"]):
            abort(409, "The export changed before it could be copied.")

        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        try:
            f = os.fdopen(fd, "rb")
        except OSError:
            os.close(fd)
            raise

        with f:
            opened = os.fstat(f.fileno())
            if not _export_matches(opened, entry["stat"]):
                abort(409, "The export changed before it could be copied.")

            remaining = entry["stat"][2]
            with target.open("wb") as out:
                while remaining:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        abort(409, "The export changed while it was being copied.")
                    out.write(chunk)
                    remaining -= len(chunk)

                extra = f.read(1)
                if extra:
                    abort(409, "The export changed while it was being copied.")

            ended = os.fstat(f.fileno())
            if not _export_matches(ended, entry["stat"]):
                abort(409, "The export changed while it was being copied.")

        after = path.lstat()
        if not _export_matches(after, entry["stat"]):
            abort(409, "The export changed after it was copied.")
    except OSError:
        abort(503, "The export could not be copied.")


def import_export(source, import_video, revision, actor, entry, destination, authorize):
    source.authorize(None, revision)
    import uuid

    key = json.dumps([revision, entry["id"], entry["rev"]], separators=(",", ":"))
    export_id = uuid.uuid5(uuid.NAMESPACE_URL, key)
    return import_video(
        actor,
        str(export_id),
        entry["name"],
        destination,
        lambda dest: save_export(source, entry, dest),
        authorize,
    )
