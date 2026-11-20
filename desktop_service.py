"""Local desktop onboarding and runtime for the packaged institution service."""

import argparse
import hmac
import json
import os
import secrets
import shutil
import sys
import tempfile
import subprocess
import threading
from pathlib import Path

from filelock import FileLock, Timeout
from flask import Flask, abort, jsonify, render_template, request
from waitress import create_server, wasyncore

import dash_transmitter
from institution import create_app
from peertube_destination import PeerTube


def save(path, values):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        json.dump(values, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if getattr(sys, "frozen", False):
        tools = Path(sys._MEIPASS) / "media-tools"
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
    if len(sys.argv) > 1 and sys.argv[1] == "--dash-transmitter":
        raise SystemExit(dash_transmitter.main(sys.argv[2:]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    home = parser.parse_args().data.resolve()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = home / "desktop-session.json"
    lock = FileLock(home / "desktop.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        if handle.exists():
            print(read(handle)["url"], flush=True)
            return
        raise RuntimeError("CuePorch is starting. Try again in a moment.")
    owner = home / "operator.json"
    config = home / "desktop-settings.json"
    token = secrets.token_urlsafe(32)
    port = int(os.environ.get("CUEPORCH_DESKTOP_PORT", "19001"))
    base = "http://127.0.0.1:" + str(port)
    shell = Flask(__name__)
    mutation = threading.Lock()
    stopping = threading.Event()
    institution = None
    settings = read(config) if config.exists() else {}

    def activate():
        keys = read(owner)
        destination = settings.get("origin") or os.environ.get(
            "CUEPORCH_PEERTUBE_ORIGIN"
        )
        if destination:
            os.environ["CUEPORCH_PEERTUBE_ORIGIN"] = destination
            if settings.get("origin"):
                os.environ.update(
                    PEERTUBE_USERNAME=settings["username"],
                    PEERTUBE_PASSWORD=settings["password"],
                )
        os.environ.update(
            CUEPORCH_STATION_KEY=keys["station"], CUEPORCH_CREATOR_KEY=keys["creator"]
        )
        collection = home / "collection"
        if not (collection / "manifest.json").exists():
            (collection / "objects").mkdir(parents=True, exist_ok=True)
            save(
                collection / "manifest.json",
                {"schema": 1, "institution": keys["institution"], "works": []},
            )
        return create_app(
            collection,
            home / "institution.sqlite",
            keys["operator"],
            keys["institution"],
            os.environ.get("CUEPORCH_SIMULATOR"),
        )

    if owner.exists():
        institution = activate()

    @shell.before_request
    def local_access():
        if request.host != "127.0.0.1:" + str(port):
            abort(403)
        if request.path.startswith("/desktop/api/") and not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + token
        ):
            abort(403)

    @shell.get("/desktop")
    def desktop():
        return render_template("desktop.html")

    @shell.get("/desktop/api/status")
    def status():
        keys = read(owner) if owner.exists() else {}
        return jsonify(
            institution=keys.get("institution"),
            operator=keys.get("operator"),
            origin=settings.get("origin") or os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"),
            data=str(home),
        )

    @shell.post("/desktop/api/setup")
    def setup():
        nonlocal institution, settings
        with mutation:
            body = request.get_json()
            name = str(body.get("institution", "")).strip()
            if not name or len(name) > 150:
                abort(400, "Enter the institution name.")
            if owner.exists() and read(owner)["institution"] != name:
                abort(409, "The existing institution name must be preserved.")
            if institution and os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"):
                abort(409, "A publishing host is already configured.")
            origin = str(body.get("origin", "")).strip().rstrip("/")
            new = {}
            if not owner.exists():
                if (home / "institution.sqlite").exists() or (
                    home / "collection/manifest.json"
                ).exists():
                    abort(
                        409,
                        "Existing data requires its original operator.json. Restore that file first.",
                    )
                save(
                    owner,
                    dict(
                        institution=name,
                        **{
                            k: secrets.token_urlsafe(32)
                            for k in ("operator", "creator", "station", "publisher")
                        },
                    ),
                )
            if body.get("localHost"):
                from deploy.bootstrap import provision as provision_host
                from provision_publisher import provision as provision_account

                if not shutil.which("docker"):
                    abort(
                        400,
                        "Start a Docker-compatible Linux container engine or connect an existing PeerTube host.",
                    )
                host = home / "host"
                try:
                    provision_host(host, 19000, 11935)
                    values = dict(
                        line.split("=", 1)
                        for line in (host / ".env").read_text().splitlines()
                        if "=" in line
                    )
                    if values.get("PUBLIC_HTTPS") == "true":
                        origin = "https://" + values["PUBLIC_HOST"]
                        if values.get("PUBLIC_PORT", "443") != "443":
                            origin += ":" + values["PUBLIC_PORT"]
                    else:
                        origin = "http://localhost:" + values.get(
                            "PUBLIC_PORT", "19000"
                        )
                    keys = read(owner)
                    username = provision_account(
                        origin, keys, values["OPERATOR_PASSWORD"]
                    )
                    new = dict(
                        origin=origin, username=username, password=keys["publisher"]
                    )
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    abort(
                        400,
                        "Could not start the private video host. Check the container engine and its storage, then retry.",
                    )
            elif origin:
                new = dict(
                    origin=origin,
                    username=str(body.get("username", "")),
                    password=str(body.get("password", "")),
                )
                try:
                    PeerTube(origin, new["username"], new["password"])
                except Exception:
                    abort(
                        400,
                        "Could not sign in to the video host. Check its address and publishing account.",
                    )
            settings = new
            save(config, settings)
            if institution:
                institution.extensions["stop_editor"]()
                institution.extensions["stop_caption_jobs"]()
                institution.extensions["stop_ai_drafts"]()
                institution.extensions["stop_file_links"]()
                institution.extensions["stop_event_capture"]()
                institution.extensions["stop_integration_watches"]()
            institution = activate()
            return jsonify(ok=True)

    @shell.post("/desktop/api/quit")
    def quit_app():
        threading.Timer(0.3, stopping.set).start()
        return jsonify(ok=True)

    def dispatch(env, start):
        path = env["PATH_INFO"]
        if (
            institution is None
            or path.startswith("/desktop")
            or path == "/static/ui/desktop.js"
        ):
            return shell(env, start)
        return institution(env, start)

    server = create_server(
        dispatch,
        host="127.0.0.1",
        port=port,
        max_request_body_size=int(
            os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", 8 * 1024**3)
        ),
    )
    try:
        url = base + "/desktop#" + token
        save(handle, {"url": url})
        print(url, flush=True)
        while not stopping.is_set():
            wasyncore.loop(timeout=0.2, count=1, map=server._map)
    finally:
        server.task_dispatcher.shutdown()
        wasyncore.close_all(server._map)
        if institution:
            institution.extensions["stop_editor"]()
            institution.extensions["stop_caption_jobs"]()
            institution.extensions["stop_ai_drafts"]()
            institution.extensions["stop_file_links"]()
            institution.extensions["stop_event_capture"]()
            institution.extensions["stop_integration_watches"]()
        handle.unlink(missing_ok=True)
        lock.release()


if __name__ == "__main__":
    main()
