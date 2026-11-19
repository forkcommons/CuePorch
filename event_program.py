"""Prepare retained cuts and start scheduled broadcasts from the institution host."""

from contextlib import suppress
import json
import os
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import uuid

from filelock import FileLock, Timeout
import requests
from werkzeug.exceptions import HTTPException

from catalog import video_version_key
from event_capture import BusyError
from library_privacy import blocked, get_privacy, protected_route
from library_timeline import find_cut, video_source
from timeline_render import Clip, render


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_program(
 event TEXT NOT NULL, opens INTEGER NOT NULL, revision TEXT NOT NULL, sources TEXT NOT NULL,
 state TEXT NOT NULL, error TEXT, PRIMARY KEY(event,opens));
"""
ACTIVE = ("starting", "broadcasting")
PENDING = ("queued", "preparing", "scheduled")
MAX_SOURCES = 20
LATE_SECONDS = 30


def program(con, uid, opens):
    return con.execute("SELECT * FROM live_program WHERE event=? AND opens=?", (uid, opens)).fetchone()


def export(row):
    if row is None:
        return dict(revision=None, state="manual", sources=[], error=None)
    return dict(revision=row["revision"], state=row["state"], sources=json.loads(row["sources"]), error=row["error"])


def permitted(con, work, visibility):
    uid = work["id"]
    if (work.get("asset_type", "video") != "video" or blocked(con, uid)
            or not any(file["role"] in ("original", "playback") for file in work["files"])):
        return False
    if visibility == "private":
        return True
    if work["provider"] == "vimeo" and work.get("source_privacy") not in ("anybody", "public"):
        return False
    policy = get_privacy(con, uid)
    if policy:
        return json.loads(policy["settings"])["visibility"] == "public"
    return protected_route(con, uid) or work.get("source_privacy") in ("public", "anybody")


class Programs:
    def __init__(self, app, tx, root, database, read_works, studio):
        self.app, self.tx, self.root, self.read_works, self.studio = app, tx, root, read_works, studio
        self.directory = database.parent / "event-programs"
        self.directory.mkdir(exist_ok=True)
        self.stopping = Event()
        self.threads = []

    def path(self, row):
        return self.directory / (uuid.UUID(row["revision"]).hex + ".mp4")

    def clean(self, row):
        with suppress(FileNotFoundError, PermissionError):
            self.path(row).unlink()

    def load(self, con, sources, visibility):
        works = {work["id"]: work for work in self.read_works()}
        result = []
        for item in sources:
            work = works.get(item["work"])
            if not work or not permitted(con, work, visibility):
                raise ValueError("A source video is unavailable for this event's audience.")
            result.append(find_cut(work, item["cut"]))
        return result

    def save(self, con, uid, opens, revision, sources, visibility):
        row = program(con, uid, opens)
        if (row["revision"] if row else None) != revision:
            raise ValueError("The program changed. Reopen it before saving.")
        if (not isinstance(sources, list) or len(sources) > MAX_SOURCES or any(
                not isinstance(item, dict) or set(item) != {"work", "cut"} or
                any(not isinstance(value, str) or len(value) > 128 for value in item.values()) for item in sources)):
            raise ValueError(f"Choose up to {MAX_SOURCES} retained videos in playback order.")
        if row and row["state"] in PENDING and json.loads(row["sources"]) == sources:
            return export(row), None
        if sources:
            self.load(con, sources, visibility)
            con.execute("INSERT OR REPLACE INTO live_program VALUES(?,?,?,?,'queued',NULL)",
                        (uid, opens, uuid.uuid4().hex, json.dumps(sources)))
        else:
            con.execute("DELETE FROM live_program WHERE event=? AND opens=?", (uid, opens))
        return export(program(con, uid, opens)), row

    def set_state(self, row, state, error=None, *, previous=None):
        with self.tx() as con:
            return con.execute(
                "UPDATE live_program SET state=?,error=? WHERE revision=? AND state=?",
                (state, error, row["revision"], previous or row["state"]),
            ).rowcount == 1

    def prepare_one(self):
        with self.tx() as con:
            row = con.execute("""SELECT p.*,e.settings FROM live_program p JOIN live_event e ON e.id=p.event
                WHERE p.state IN ('queued','preparing') ORDER BY p.opens,p.event LIMIT 1""").fetchone()
            if not row:
                return
            con.execute("UPDATE live_program SET state='preparing' WHERE revision=?", (row["revision"],))
        try:
            with self.tx() as con:
                cuts = self.load(con, json.loads(row["sources"]), json.loads(row["settings"])["visibility"])
            clips = []
            for cut in cuts:
                source, info = video_source(self.root, cut)
                clips.append(Clip(source, 0, round(info["duration"] * 1000), bool(info["audio"])))
            with TemporaryDirectory(dir=self.directory, prefix="prepare-") as name:
                output = Path(name) / "program.mp4"
                render(output, clips, (1280, 720), "182629", stopping=self.stopping)
                os.replace(output, self.path(row))
                if not self.set_state(row, "scheduled", previous="preparing"):
                    self.clean(row)
        except InterruptedError:
            return
        except (ValueError, HTTPException) as error:
            self.set_state(row, "failed", error.description if isinstance(error, HTTPException) else str(error),
                           previous="preparing")
        except (OSError, TimeoutExpired):
            self.set_state(row, "failed", "CuePorch could not prepare the program's media.", previous="preparing")

    def run_due(self, lock, qualify, prepare_host, running):
        for revision, (row, room) in list(running.items()):
            if room.done.is_set():
                complete = room.process.returncode == 0 and not room.stop.is_set()
                self.set_state(row, "finished" if complete else "interrupted",
                               None if complete else "The program stopped before completing.", previous="broadcasting")
                self.clean(row)
                del running[revision]
        with self.tx() as con:
            rows = con.execute("""SELECT * FROM live_program WHERE opens<=? AND state IN
                ('queued','preparing','scheduled','starting','broadcasting') ORDER BY opens,event""", (time.time(),)).fetchall()
        for row in rows:
            if self.stopping.is_set():
                return
            uid, opens, revision = row["event"], row["opens"], row["revision"]
            if revision in running:
                continue
            try:
                with lock(uid):
                    with self.tx() as con:
                        held = program(con, uid, opens)
                        if held is None or held["revision"] != revision or held["state"] != row["state"]:
                            continue
                        if row["state"] in ACTIVE:
                            con.execute("UPDATE live_program SET state='interrupted',error=? WHERE revision=?",
                                        ("CuePorch restarted during this one-time broadcast.", revision))
                            self.clean(row)
                            continue
                        if time.time() >= opens + LATE_SECONDS:
                            con.execute("UPDATE live_program SET state='missed',error=? WHERE revision=?",
                                        ("The program was not ready to broadcast at its scheduled time.", revision))
                            self.clean(row)
                            continue
                        if row["state"] != "scheduled" or not qualify(con, uid, opens):
                            continue
                        settings = con.execute("SELECT settings FROM live_event WHERE id=?", (uid,)).fetchone()[0]
                        self.load(con, json.loads(row["sources"]), json.loads(settings)["visibility"])
                    if not self.path(row).is_file():
                        self.set_state(row, "queued")
                        continue
                    encoder = prepare_host(uid, opens, browser=True, automatic=True)
                    if self.stopping.is_set():
                        return
                    with self.tx() as con:
                        self.load(con, json.loads(row["sources"]), json.loads(settings)["visibility"])
                    if not self.set_state(row, "starting"):
                        continue
                    room = self.studio.program(uid, opens, encoder, self.path(row))
                    running[revision] = (row, room)
                    self.set_state(row, "broadcasting", previous="starting")
            except (Timeout, BusyError, requests.RequestException):
                self.set_state(row, "scheduled", previous="starting")
            except (ValueError, HTTPException, OSError) as error:
                if isinstance(error, HTTPException) and error.code == 502:
                    continue
                issue = error.description if isinstance(error, HTTPException) else str(error)
                for prior in (row["state"], "starting"):
                    if self.set_state(row, "failed", issue, previous=prior):
                        self.clean(row)
                        break

    def start(self, lock, qualify, prepare_host):
        running = {}

        def loop(name, step):
            lease = FileLock(self.directory / (name + ".lock"), timeout=0)
            with self.app.app_context():
                while not self.stopping.is_set():
                    try:
                        with lease:
                            if name == "render":
                                step()
                            else:
                                while not self.stopping.is_set():
                                    step()
                                    self.stopping.wait(1)
                    except Timeout:
                        pass
                    except Exception:
                        self.app.logger.exception("The prerecorded event worker could not read its queue")
                    self.stopping.wait(1)

        for name, step in (("render", self.prepare_one), ("broadcast", lambda: self.run_due(lock, qualify, prepare_host, running))):
            thread = Thread(target=loop, args=(name, step), name=f"CuePorch program {name}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=2)
