"""Generate a missing caption track with the operator's local TextFlowKit installation."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cueporch import checksum


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


def caption_work(work, collection):
    if any(f["role"] == "caption" for f in work["files"]):
        return
    media = next(
        (f for f in work["files"] if f["role"] in ("playback", "original")), None
    )
    if not media:
        return
    args, env = command()
    if not args:
        work["caption_status"] = "missing; no local TextFlowKit installation"
        return
    with tempfile.TemporaryDirectory(dir=collection.parent) as temp:
        folder = Path(temp)
        src = folder / ("source." + media["format"])
        original = collection / "objects" / media["sha256"]
        try:
            os.link(original, src)
        except OSError:
            shutil.copyfile(original, src)
        output = folder / "tracks"
        result = subprocess.run(
            args
            + [
                "transcribe",
                str(src),
                "--formats",
                "vtt,json",
                "--engine",
                "whistle",
                "--output-dir",
                str(output),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=int(env.get("CUEPORCH_CAPTION_TIMEOUT", "14400")),
        )
        if result.returncode:
            work["caption_status"] = "TextFlowKit transcription failed"
            return
        vtts = list(output.rglob("*.vtt"))
        transcripts = list(output.rglob("*.json"))
        if len(vtts) != 1 or len(transcripts) != 1:
            work["caption_status"] = "TextFlowKit output was incomplete"
            return
        record = json.loads(transcripts[0].read_text(encoding="utf-8-sig"))
        for file, role in [
            (vtts[0], "caption"),
            (transcripts[0], "caption-provenance"),
        ]:
            key = checksum(file)
            dest = collection / "objects" / key
            file.replace(dest)
            work["files"].append(
                dict(
                    role=role,
                    format=file.suffix[1:],
                    sha256=key,
                    bytes=dest.stat().st_size,
                    language=record.get("language") or "und",
                    provenance="TextFlowKit machine-generated",
                )
            )
        work["caption_status"] = "machine-generated"
