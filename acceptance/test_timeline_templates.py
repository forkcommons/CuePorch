import json
import secrets
import sqlite3
import subprocess
import sys
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
from export_institution import export


class TimelineTemplates(unittest.TestCase):
    def test_templates(self):
        self.run_fixture(check_timeline_templates)

    def test_graphics(self):
        self.run_fixture(check_timeline_graphics)

    def test_stock(self):
        self.run_fixture(check_timeline_stock)

    def run_fixture(self, check):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory())) / "collection"
            (root / "objects").mkdir(parents=True)
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            database = root.parent / "workspace.sqlite"
            sqlite3.connect(database).close()
            operator = secrets.token_urlsafe(32)

            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"

            cleanup.enter_context(
                patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": base})
            )
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            service.app = app
            cleanup.callback(
                lambda: [
                    stop()
                    for name, stop in list(app.extensions.items())
                    if name.startswith("stop_")
                ]
            )
            cleanup.enter_context(service_worker(service))

            clip = root.parent / "blue.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:size=320x180:rate=30:d=2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                check=True,
            )
            auth = {"Authorization": f"Bearer {operator}"}
            with clip.open("rb") as file:
                response = requests.post(
                    base + "/api/library/uploads",
                    headers=auth,
                    data={"upload_id": str(uuid.uuid4())},
                    files={"file": ("blue.mp4", file, "video/mp4")},
                    timeout=30,
                )
            response.raise_for_status()
            uid = response.json()["id"]

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.add_init_script(
                    f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)})"
                )
                check(page, base, auth, uid, root)


def check_timeline_templates(page, base, auth, uid, tmp):
    work = f"{base}/api/library/work/{uid}"

    cut = requests.get(work + "/timeline", headers=auth, timeout=15).json()["current"]
    response = requests.post(
        work + "/languages",
        headers=auth,
        timeout=15,
        data={"version": cut, "language": "en"},
        files={"track": ("blue.vtt", b"WEBVTT\n\n00:00:00.250 --> 00:00:00.750\nBlue caption\n", "text/vtt")},
    )
    response.raise_for_status()

    guest = requests.get(f"{base}/api/library/timeline-templates", timeout=30)
    assert guest.status_code == 403, guest.status_code
    r = requests.get(f"{base}/api/library/timeline-templates", headers=auth, timeout=30)
    r.raise_for_status()
    templates = r.json()["templates"]
    assert len(templates) == 35
    assert len({t["category"] for t in templates}) == 35
    assert len({t["id"] for t in templates}) == 35
    tpl = next(t for t in templates if t["id"] == "gallery-wall")
    assert tpl["category"] == "Museum", tpl.get("category")
    opening = tpl["opening"]["card"]["title"]
    closing = tpl["closing"]["card"]["title"]
    opening_bg = bytes.fromhex(tpl["opening"]["card"]["background"])
    closing_bg = bytes.fromhex(tpl["closing"]["card"]["background"])

    page.goto(f"{base}/operator/videos/{uid}/timeline")
    expect(page.locator("#timelineTemplate")).to_be_enabled(timeout=30000)

    page.fill("#timelineTemplateSearch", "zzz-absent-template-zzz")
    expect(page.locator("text=No templates match the current filters.")).to_be_visible(timeout=15000)
    page.fill("#timelineTemplateSearch", "")
    page.select_option("#timelineTemplateCategory", "Museum")
    page.select_option("#timelineTemplateRatio", "9:16")
    expect(page.locator("#timelineTemplateOpening")).to_contain_text(opening)
    expect(page.locator("#timelineTemplateClosing")).to_contain_text(closing)

    clips = page.locator("#timelineClips > li")
    page.click("#timelineTemplateInsertOpening")
    expect(clips).to_have_count(2, timeout=15000)
    page.click("#undoTimeline")
    expect(clips).to_have_count(1, timeout=15000)
    page.click("#timelineTemplateApply")
    expect(clips).to_have_count(3, timeout=15000)
    expect(page.locator("#timelineRatio")).to_have_value("9:16")

    clips.nth(0).get_by_label("Scene title").fill("Gallery after hours")
    clips.nth(0).get_by_label("Scene title").press("Tab")
    clips.nth(0).get_by_role("button", name="Gallery after hours").click()
    expect(page.locator("#timelineCardPreview")).to_contain_text("Gallery after hours")

    expect(page.locator("text=Draft saved.")).to_be_visible(timeout=30000)
    page.reload()
    expect(page.locator("#timelineClips > li")).to_have_count(3, timeout=30000)
    expect(clips.nth(0).get_by_label("Scene title")).to_have_value("Gallery after hours")

    page.click("#renderTimeline")
    expect(page.locator("text=New version saved.")).to_be_visible(timeout=60000)

    r = requests.get(work + "/languages/en", headers=auth, timeout=15)
    r.raise_for_status()
    cues = r.json()["cues"]
    assert [(c["start"], c["end"], c["text"]) for c in cues] == [(4250, 4750, "Blue caption")]

    d = requests.get(f"{work}/details", headers=auth, timeout=30).json()
    assert d["media"]["width"] == 720 and d["media"]["height"] == 1280, d.get("media")

    cur = requests.get(f"{work}/timeline", headers=auth, timeout=30).json()["current"]
    pv = requests.post(f"{work}/timeline/preview", json={"cut": cur}, headers=auth, timeout=60)
    pv.raise_for_status()
    prev_path = tmp / "preview.mp4"
    with requests.get(base + pv.json()["preview"], headers=auth, timeout=300, stream=True) as resp:
        resp.raise_for_status()
        with open(prev_path, "wb") as f:
            for c in resp.iter_content(1 << 15):
                f.write(c)

    def avg(t, x, y):
        raw = subprocess.check_output([
            "ffmpeg", "-v", "error", "-ss", str(t), "-i", str(prev_path), "-vf", f"crop=2:2:{x}:{y}",
            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"
        ])
        n = len(raw) // 3
        return tuple(sum(raw[i::3]) // n for i in range(3))

    for t, kind in ((1, "opening"), (4.5, "source"), (7, "closing")):
        if kind == "source":
            rgb = avg(t, 360, 640)
            assert rgb[2] > 180 and rgb[0] < 80 and rgb[1] < 80, (kind, rgb)
        else:
            target = opening_bg if kind == "opening" else closing_bg
            rgb = avg(t, 696, 8)
            assert all(abs(rgb[i] - target[i]) <= 18 for i in range(3)), (kind, rgb, target)


def check_timeline_graphics(page, base, auth, uid, root):
    work = f"{base}/api/library/work/{uid}"

    guest = requests.get(base + "/api/library/timeline-graphics", timeout=15)
    assert guest.status_code == 403, guest.status_code

    r = requests.get(base + "/api/library/timeline-graphics", headers=auth, timeout=15)
    r.raise_for_status()
    body = r.json()
    assets = body["assets"]
    assert isinstance(assets, list) and len(assets) == 9, assets
    assert any(a.get("id") == "frame" for a in assets), assets

    page.goto(f"{base}/operator/videos/{uid}/timeline")
    clips = page.locator("#timelineClips > li")
    expect(clips).to_have_count(1, timeout=30000)
    clip_button = clips.first.get_by_role("button", name="blue")
    if clip_button.count() == 0:
        clip_button = clips.first.locator("button").first
    clip_button.click()
    expect(page.locator("#timelinePreview")).to_be_visible(timeout=30000)

    page.select_option("#timelineGraphic", "frame")
    page.fill("#timelineGraphicColor", "#ee9900")
    page.click("#applyTimelineGraphic")
    expect(page.locator("#timelineGraphicPreview")).to_be_visible(timeout=30000)
    expect(page.locator("text=Draft saved.")).to_be_visible(timeout=30000)

    page.click("#undoTimeline")
    expect(page.locator("#timelineGraphicPreview")).to_be_hidden(timeout=30000)
    page.click("#redoTimeline")
    clip_button.click()
    expect(page.locator("#timelineGraphicPreview")).to_be_visible(timeout=30000)
    page.click("#saveTimeline")
    expect(page.locator("text=Draft saved.")).to_be_visible(timeout=30000)

    page.reload()
    expect(clips).to_have_count(1, timeout=30000)
    clip_button = clips.first.get_by_role("button", name="blue")
    if clip_button.count() == 0:
        clip_button = clips.first.locator("button").first
    clip_button.click()
    expect(page.locator("#timelineGraphic")).to_have_value("frame")
    expect(page.locator("#timelineGraphicPreview")).to_be_visible(timeout=30000)

    page.click("#renderTimeline")
    expect(page.locator("text=New version saved.")).to_be_visible(timeout=60000)

    manifest = json.loads((root / "manifest.json").read_text())
    work_entry = next(
        w for w in manifest["works"] if w.get("id") == uid or w.get("uid") == uid
    )
    expected_assets = [{"id": "frame", "source": "cueporch-built-in", "revision": 1}]
    assert work_entry["editor_assets"] == expected_assets, work_entry.get("editor_assets")

    with TemporaryDirectory() as export_tmp:
        export_target = Path(export_tmp) / "portable"
        export(root, root.parent / "workspace.sqlite", export_target)
        portable = json.loads((export_target / "collection" / "manifest.json").read_text())
        portable_work = next(
            w for w in portable["works"] if w.get("id") == uid or w.get("uid") == uid
        )
        assert portable_work["editor_assets"] == expected_assets, portable_work.get("editor_assets")

    cur = requests.get(work + "/timeline", headers=auth, timeout=15).json()["current"]
    pv = requests.post(work + "/timeline/preview", json={"cut": cur}, headers=auth, timeout=60)
    pv.raise_for_status()
    graphic = root / "graphic.mp4"
    with requests.get(base + pv.json()["preview"], headers=auth, timeout=300, stream=True) as resp:
        resp.raise_for_status()
        with graphic.open("wb") as f:
            for chunk in resp.iter_content(1 << 15):
                f.write(chunk)

    def avg(t, x, y):
        raw = subprocess.check_output([
            "ffmpeg", "-v", "error", "-ss", str(t), "-i", str(graphic),
            "-vf", f"crop=2:2:{x}:{y}", "-frames:v", "1",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"
        ])
        assert len(raw) >= 3, (t, x, y, len(raw))
        n = len(raw) // 3
        return tuple(sum(raw[i::3]) // n for i in range(3))

    rgb = avg(0.5, 4, 4)
    assert all(abs(actual - expected) <= 24 for actual, expected in zip(rgb, (0xee, 0x99, 0x00))), rgb

    rgb = avg(1.0, 640, 360)
    assert rgb[2] > 180 and rgb[0] < 80 and rgb[1] < 80, rgb


def check_timeline_stock(page, base, auth, uid, root):
    import subprocess

    wave_path = root.parent / "wave.wav"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1",
            "-ac", "2", "-ar", "44100",
            str(wave_path),
        ],
        check=True,
    )

    page.goto(f"{base}/operator/videos/{uid}/timeline")
    page.get_by_text("Add a licensed file").click()
    page.locator("#timelineStockFile").set_input_files(str(wave_path))
    page.locator("#timelineStockUploadCategory").fill("Music")
    page.locator("#timelineStockUploadMood").fill("Calm")
    page.locator("#timelineStockUploadGenre").fill("Ambient")
    page.locator("#timelineStockLicense").select_option("CC-BY-4.0")
    page.locator("#timelineStockAuthor").fill("Studio Fixture")
    page.locator("#timelineStockSource").fill("https://example.test/fixture")
    page.locator("#timelineStockUploadButton").click()
    expect(page.locator("#timelineStockCredit")).to_contain_text("Studio Fixture")

    page.locator("#timelineStockKind").select_option("audio")
    page.locator("#timelineStockMood").select_option("Calm")
    chosen_asset = page.locator("#timelineStockAsset").input_value()
    page.locator("#timelineStockUse").click()
    expect(page.locator("#timelineStatus")).to_have_text("Draft saved.")

    resp = requests.get(f"{base}/api/library/work/{uid}/timeline", headers=auth, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    assert payload["settings"]["music"]["work"] == chosen_asset

    page.locator("#renderTimeline").click()
    expect(page.locator("#timelineStatus")).to_have_text("New version saved.", timeout=60000)

    check_stock_output(base, auth, uid, root)

    editing = requests.get(
        f"{base}/api/library/work/{uid}/editing",
        headers=auth,
        timeout=15,
    ).json()
    key = editing["version"]
    trim = requests.post(
        f"{base}/api/library/work/{uid}/editing/trim",
        headers=auth,
        json=dict(version=key, start=0, end=700),
        timeout=30,
    )
    trim.raise_for_status()
    check_stock_output(base, auth, uid, root)

    response = requests.put(
        base + f"/api/library/work/{uid}/privacy",
        headers=auth,
        json=dict(visibility="public", embed="anywhere", domains=[], download=True),
        timeout=15,
    )
    response.raise_for_status()

    page.goto(base + f"/watch/work/{uid}")
    page.get_by_text("Media credits", exact=True).click()

    details = page.locator("details").filter(has_text="Media credits")
    expect(details).to_contain_text("Studio Fixture")
    expect(details.get_by_role("link", name="Source")).to_have_attribute(
        "href", "https://example.test/fixture"
    )
    expect(details.get_by_role("link", name="License")).to_have_attribute(
        "href", "https://creativecommons.org/licenses/by/4.0/"
    )

    check_stock_upload_validation(base, auth, root.parent / "blue.mp4")


def check_stock_output(base, auth, uid, root):
    root = Path(root)

    manifest_path = root / "manifest.json"
    assert manifest_path.exists(), "manifest.json missing"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    works = manifest.get("works") or []

    local_work = None
    for w in works:
        if w.get("id") == uid:
            local_work = w
            break
    assert local_work is not None, f"uid {uid} not present in local manifest"

    editor_assets = local_work.get("editor_assets") or []
    stock = [a for a in editor_assets if a.get("source") == "cueporch-stock"]
    assert len(stock) == 1, "expected exactly one cueporch-stock editor_asset"
    asset = stock[0]
    assert asset.get("credit") == "Studio Fixture", f"unexpected credit: {asset.get('credit')!r}"
    assert asset.get("license") == "CC-BY-4.0", f"unexpected license: {asset.get('license')!r}"
    assert asset.get("source_url") == "https://example.test/fixture", (
        f"unexpected source_url: {asset.get('source_url')!r}"
    )
    assert not asset.get("permission_note"), f"unexpected permission_note: {asset.get('permission_note')!r}"

    files = local_work.get("files") or []
    orig_files = [f for f in files if f.get("role") == "original"]
    play_files = [f for f in files if f.get("role") == "playback"]
    assert orig_files, "missing original file"
    assert play_files, "missing playback file"

    for f in files:
        role = f.get("role")
        sha = f.get("sha256")
        assert role in ("original", "playback"), f"unexpected role: {role!r}"
        assert sha, f"missing sha256 for role {role}"
        path = root / "objects" / sha
        assert path.exists(), f"missing {role} object: {sha}"
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path)],
            capture_output=True,
        )
        text = proc.stderr.decode("utf-8", errors="replace")
        assert "Credit: Studio Fixture" in text, f"missing credit comment in {role} {sha}"

    orig_sha = orig_files[0]["sha256"]
    orig_path = root / "objects" / orig_sha
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(orig_path),
            "-t", "0.2", "-f", "s16le", "-ac", "1", "-ar", "44100", "pipe:1",
        ],
        capture_output=True,
    )
    decoded = proc.stdout
    assert decoded, "ffmpeg produced no decoded audio from original"
    assert any(b != 0 for b in decoded), "decoded audio has zero energy"

    with TemporaryDirectory() as tmp:
        target = Path(tmp) / "stock-export"
        assert not target.resolve().is_relative_to(root.resolve()), (
            "temporary target unexpectedly inside root"
        )
        export(root, root.parent / "workspace.sqlite", target)
        exported_manifest_path = target / "collection" / "manifest.json"
        assert exported_manifest_path.exists(), (
            "export did not produce collection/manifest.json"
        )
        exported = json.loads(exported_manifest_path.read_text(encoding="utf-8"))
        exported_work = None
        for w in exported.get("works") or []:
            if w.get("id") == uid:
                exported_work = w
                break
        assert exported_work is not None, f"exported manifest missing uid {uid}"

        local_file_sig = sorted((f.get("role"), f.get("sha256")) for f in files)
        export_file_sig = sorted(
            (f.get("role"), f.get("sha256")) for f in (exported_work.get("files") or [])
        )
        assert local_file_sig == export_file_sig, "exported files do not match local files"

        local_asset_sig = sorted(
            (a.get("source"), a.get("credit"), a.get("license"), a.get("source_url"))
            for a in editor_assets
        )
        export_asset_sig = sorted(
            (a.get("source"), a.get("credit"), a.get("license"), a.get("source_url"))
            for a in (exported_work.get("editor_assets") or [])
        )
        assert local_asset_sig == export_asset_sig, "exported editor_assets do not match"


def check_stock_upload_validation(base, auth, clip):
    identity = str(uuid.uuid4())
    def post(identity, metadata):
        return requests.post(base+"/api/library/uploads",headers=auth, files={"file":(clip.name,clip.read_bytes(),"video/mp4")},data={"upload_id":identity,"stock_asset":json.dumps(metadata)},timeout=20)
    valid = {"category":"Fixture","credit":"Station Fixture","license":"permission","permission_note":"private-note-fixture"}
    r = post(identity, valid)
    assert r.status_code == 201
    asset_id = r.json()["id"]
    assert post(identity, valid).status_code == 200
    assert post(identity, {**valid, "credit":"Changed Credit"}).status_code == 409
    assert post(str(uuid.uuid4()), {**valid, "license":["permission"]}).status_code == 422
    cc = {"category":"Fixture","credit":"Station Fixture","license":"CC-BY-4.0"}
    assert post(str(uuid.uuid4()), cc).status_code == 422
    assert post(str(uuid.uuid4()), {**cc, "source_url":"javascript:alert(1)"}).status_code == 422
    library = requests.get(base+"/api/library", headers=auth, timeout=20).json()
    records = library.get("items", library) if isinstance(library, dict) else library
    record = next(item for item in records if item["id"] == asset_id)
    stock = record["stock_asset"]
    if isinstance(stock, str):
        stock = json.loads(stock)
    assert "permission_note" not in stock
