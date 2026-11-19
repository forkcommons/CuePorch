"""Retain private PDF and PowerPoint slides for the Live Studio compositor."""

import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
import uuid
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from flask import abort, request, send_file
import pypdfium2 as pdfium

from media_tools import run_media


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_slide_deck(
 event TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, pages INTEGER NOT NULL,
 format TEXT NOT NULL, PRIMARY KEY(event,id));
"""
MAX_SIZE = 100 * 1024**2
MAX_PAGES = 500
IDENTIFIER = re.compile(r"[a-f0-9]{32}")
RENDERING = Lock()


def decks(con, event):
    return {row["id"]: dict(row) for row in con.execute(
        "SELECT id,title,pages,format FROM live_slide_deck WHERE event=? ORDER BY title COLLATE NOCASE,id",
        (event,),
    )}


def libreoffice():
    configured = os.environ.get("CUEPORCH_SOFFICE")
    if configured:
        return configured if Path(configured).is_file() else None
    command = shutil.which("soffice.com" if os.name == "nt" else "soffice")
    if command:
        return command
    windows = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice/program/soffice.com"
    candidates = (windows,) if os.name == "nt" else (Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),)
    return next((str(path) for path in candidates if path.is_file()), None)


def validate_powerpoint(source):
    with ZipFile(source) as archive:
        items = archive.infolist()
        if (len(items) > 15000 or sum(info.file_size for info in items) > 600 * 1024**2
                or "ppt/presentation.xml" not in archive.namelist()):
            raise ValueError("Choose a standard PowerPoint deck with embedded media.")
        for item in items:
            if not item.filename.endswith(".rels"):
                continue
            if item.file_size > 1024**2:
                raise ValueError("The PowerPoint file contains oversized relationship metadata.")
            for relation in ElementTree.fromstring(archive.read(item)):
                if relation.get("TargetMode", "").lower() == "external" and not relation.get("Type", "").endswith("/hyperlink"):
                    raise ValueError("Embed externally linked media in PowerPoint before adding the deck.")


def convert_powerpoint(source, scratch):
    validate_powerpoint(source)
    command = libreoffice()
    if not command:
        raise ValueError("Install LibreOffice Impress on the CuePorch computer to import PowerPoint. You can also upload PDF.")
    result = run_media([command, "-env:UserInstallation=" + (scratch / "office").as_uri(),
                        "--headless", "--nologo", "--nodefault", "--norestore",
                        "--convert-to", "pdf:impress_pdf_Export", "--outdir", str(scratch), str(source)], timeout=120)
    converted = scratch / "source.pdf"
    if result.returncode or not converted.is_file():
        raise ValueError("LibreOffice could not open this PowerPoint deck.")
    return converted


def render_slides(source, destination):
    with pdfium.PdfDocument(source) as document:
        count = len(document)
        if not 1 <= count <= MAX_PAGES:
            raise ValueError(f"Upload a presentation with up to {MAX_PAGES} slides.")
        for index in range(count):
            page = document[index]
            try:
                width, height = page.get_size()
                if not (width > 0 and height > 0):
                    raise ValueError("The deck contains a slide with an invalid size.")
                bitmap = page.render(scale=1280 / max(width, height))
                try:
                    bitmap.to_pil().convert("RGB").save(destination / f"{index}.png")
                finally:
                    bitmap.close()
            finally:
                page.close()
    return count


def retain(upload, storage, identity, extension, save):
    if not RENDERING.acquire(blocking=False):
        abort(409, "Another presentation is being imported. Try again when it finishes.")
    try:
        storage.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=storage) as draft:
            scratch = Path(draft)
            deck = scratch / "deck"
            deck.mkdir()
            source = deck / f"source{extension}"
            total = 0
            with source.open("wb") as target:
                for chunk in iter(lambda: upload.stream.read(256 * 1024), b""):
                    total += len(chunk)
                    if total > MAX_SIZE:
                        abort(413, "Upload a presentation of up to 100 MB.")
                    target.write(chunk)
            if not total:
                abort(422, "Choose a PDF or PowerPoint file.")
            pdf = convert_powerpoint(source, scratch) if extension == ".pptx" else source
            pages = render_slides(pdf, deck)
            kept = storage / identity
            deck.rename(kept)
            try:
                save(pages)
            except Exception:
                shutil.rmtree(kept)
                raise
            return pages
    finally:
        RENDERING.release()


def register(app, tx, staff, database, access):
    storage = database.parent / "live-slide-decks"

    @app.post("/api/live-events/<uid>/<int:opens>/scenes/slides")
    def import_studio_slides(uid, opens):
        staff()
        with tx() as con:
            access(con, uid, opens)
        request.max_content_length = MAX_SIZE + 1024**2
        upload = request.files.get("deck")
        name = PurePosixPath(upload.filename.replace("\\", "/")) if upload and upload.filename else None
        extension = name.suffix.lower() if name else ""
        if extension not in (".pdf", ".pptx"):
            abort(422, "Choose a PDF or PowerPoint file.")
        identity, title, format_name = uuid.uuid4().hex, name.stem[:120], extension[1:]

        def save(pages):
            with tx() as con:
                access(con, uid, opens)
                con.execute("INSERT INTO live_slide_deck VALUES(?,?,?,?,?)", (uid, identity, title, pages, format_name))

        try:
            pages = retain(upload, storage, identity, extension, save)
        except ValueError as error:
            abort(422, str(error))
        except (BadZipFile, ElementTree.ParseError, pdfium.PdfiumError):
            abort(422, "The presentation could not be read. Check that it opens without a password.")
        except (OSError, subprocess.TimeoutExpired):
            abort(422, "The presentation could not be prepared. Check the file and available storage.")
        return dict(id=identity, title=title, pages=pages, format=format_name), 201

    @app.get("/api/live-events/<uid>/<int:opens>/scenes/slides/<identity>/<int:page>")
    def studio_slide_image(uid, opens, identity, page):
        staff()
        if not IDENTIFIER.fullmatch(identity):
            abort(404)
        with tx() as con:
            access(con, uid, opens)
            entry = con.execute("SELECT pages FROM live_slide_deck WHERE event=? AND id=?", (uid, identity)).fetchone()
            if not entry or page >= entry["pages"]:
                abort(404)
        path = storage / identity / f"{page}.png"
        if not path.is_file():
            abort(404)
        response = send_file(path, mimetype="image/png", conditional=True)
        response.cache_control.private = True
        response.cache_control.no_store = True
        return response
