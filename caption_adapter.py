"""Generate a missing caption track with the operator's local TextFlowKit installation."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from cueporch import checksum


class CaptionError(ValueError):
    pass


TRANSLATE = """
import json
import sys

from textflowkit.core.translate import get_translator

request = json.loads(sys.stdin.buffer.read())
result = get_translator().translate(request["texts"], request["language"])
sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
"""


def translation_runtime():
    env = dict(os.environ)
    model = env.get("TEXTFLOWKIT_TRANSLATE_MODEL", "").strip()
    try:
        host = urlsplit(env.get("TEXTFLOWKIT_OLLAMA_HOST", "http://127.0.0.1:11434"))
    except ValueError:
        return None
    if (not model or model.casefold().endswith(":cloud") or host.scheme not in ("http", "https")
            or host.hostname not in ("localhost", "127.0.0.1", "::1") or host.username is not None
            or host.path not in ("", "/") or host.query or host.fragment):
        return None
    python = env.get("CUEPORCH_TEXTFLOWKIT_PYTHON")
    if not python:
        args, installed_env = command()
        if len(args) > 1 and args[1] == "-c":
            python, env = args[0], installed_env
        elif args:
            directory = Path(args[0]).parent
            python = directory / ("python.exe" if os.name == "nt" else "python")
    if not python or not Path(python).is_file():
        return None
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return [str(python), "-c", TRANSLATE], env


def translate_cues(cues, language, stopping=None):
    runtime = translation_runtime()
    if runtime is None:
        raise CaptionError("Configure a TextFlowKit runtime and local translation model first.")
    args, env = runtime
    body = json.dumps(dict(texts=[cue["text"] for cue in cues], language=language)).encode("utf-8")
    try:
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as result:
            source.write(body)
            source.seek(0)
            code = wait_for_transcription(args, env, int(env.get("CUEPORCH_CAPTION_TIMEOUT", "14400")),
                                          stopping, stdin=source, stdout=result)
            if code:
                raise CaptionError("The translation runtime did not complete.")
            result.seek(0)
            texts = json.load(result)
    except (subprocess.SubprocessError, UnicodeError, ValueError) as error:
        raise CaptionError("TextFlowKit could not translate this track using the configured local model.") from error
    if not isinstance(texts, list) or len(texts) != len(cues):
        raise CaptionError("The local model returned an incomplete subtitle translation.")
    return [dict(cue, text=text) for cue, text in zip(cues, texts, strict=True)]


def command():
    env = dict(os.environ)
    explicit = env.get("CUEPORCH_TEXTFLOWKIT") or shutil.which("textflowkit")
    if explicit:
        return [explicit], env
    if os.name == "nt" and env.get("LOCALAPPDATA"):
        root = Path(env["LOCALAPPDATA"]) / "Programs" / "TextFlowKit"
        python = root / "runtime" / "python.exe"
        if python.is_file():
            env["PATH"] = str(root / "bin") + os.pathsep + env.get("PATH", "")
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            env.setdefault("TEXTFLOWKIT_MODELS_DIR", str(root / "models"))
            env.setdefault("TEXTFLOWKIT_OFFLINE", "1")
            return [
                str(python),
                "-c",
                "from textflowkit.cli import main;raise SystemExit(main())",
            ], env
    return [], env


def wait_for_transcription(args, env, timeout, stopping, *, stdin=None, stdout=subprocess.DEVNULL):
    options = dict(env=env, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if stopping is None:
        return subprocess.run(args, timeout=timeout, **options).returncode
    deadline = monotonic() + timeout
    with subprocess.Popen(args, **options) as process:
        try:
            while process.poll() is None:
                if stopping.wait(0.2):
                    raise CaptionError("The transcription was interrupted.")
                if monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("TextFlowKit", timeout)
            return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def transcribe_path(source, output, language=None, stopping=None):
    args, env = command()
    if not args:
        raise CaptionError("missing; no local TextFlowKit installation")
    options = ["--language", language.split("-", 1)[0]] if language else []
    result = wait_for_transcription(
        args + ["transcribe", str(source), "--formats", "vtt,json", "--engine", "whistle",
                "--output-dir", str(output)] + options,
        env, int(env.get("CUEPORCH_CAPTION_TIMEOUT", "14400")), stopping,
    )
    if result:
        raise CaptionError("TextFlowKit transcription failed")
    vtts = list(output.rglob("*.vtt"))
    transcripts = list(output.rglob("*.json"))
    if len(vtts) != 1 or len(transcripts) != 1:
        raise CaptionError("TextFlowKit output was incomplete")
    return vtts[0].read_bytes(), transcripts[0].read_bytes()


def transcribe_caption(collection, media, language=None, stopping=None):
    with tempfile.TemporaryDirectory(dir=collection.parent) as temp:
        folder = Path(temp)
        src = folder / ("source." + media["format"])
        original = collection / "objects" / media["sha256"]
        try:
            os.link(original, src)
        except OSError:
            shutil.copyfile(original, src)
        return transcribe_path(src, folder / "tracks", language, stopping)


def caption_work(work, collection):
    if any(f["role"] == "caption" for f in work["files"]):
        return
    media = next(
        (f for f in work["files"] if f["role"] in ("playback", "original")), None
    )
    if not media:
        return
    try:
        vtt, transcript = transcribe_caption(collection, media)
    except CaptionError as error:
        work["caption_status"] = str(error)
        return
    record = json.loads(transcript.decode("utf-8-sig"))
    for data, suffix, role in [(vtt, "vtt", "caption"), (transcript, "json", "caption-provenance")]:
        with tempfile.NamedTemporaryFile(dir=collection / "objects", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
        try:
            key = checksum(staged)
            dest = collection / "objects" / key
            staged.replace(dest)
        finally:
            staged.unlink(missing_ok=True)
        work["files"].append(
            dict(role=role, format=suffix, sha256=key, bytes=len(data),
                 language=record.get("language") or "und", provenance="TextFlowKit machine-generated")
        )
    work["caption_status"] = "machine-generated"
