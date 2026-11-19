"""Read text subtitle files into editable millisecond-precision cues."""

import re

LIMIT = 20000
TIME = re.compile(r"(?:(\d{1,3}):)?([0-5]\d):([0-5]\d)[.,](\d{3})")
TIMING = re.compile(r"(\S+)\s+-->\s+(\S+)(?:[ \t]+([^\n]*))?")
LANGUAGE = re.compile(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8}){0,5}")


def timestamp(value):
    match = TIME.fullmatch(value)
    if not match:
        raise ValueError("Use a valid subtitle timestamp, such as 00:00:01.500.")
    hours, minutes, seconds, milliseconds = (int(part or 0) for part in match.groups())
    return (hours * 3600 + minutes * 60 + seconds) * 1000 + milliseconds


def formatted(value):
    seconds, fraction = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}"


def validate(cues):
    if not isinstance(cues, list) or len(cues) > LIMIT:
        raise ValueError(f"A subtitle track can contain up to {LIMIT:,} cues.")
    seen = []
    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError("Each cue needs a start time, end time and text.")
        start, end = cue.get("start"), cue.get("end")
        text = cue.get("text")
        settings = cue.get("settings", "")
        if type(start) is not int or type(end) is not int or not 0 <= start < end < 3600000000:
            raise ValueError("Each end time must come after its cue's start time.")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000 or "\x00" in text:
            raise ValueError("Each cue needs text of up to 4,000 characters.")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if re.search(r"\n[ \t]*\n", text) or not isinstance(settings, str) or len(settings) > 200 or any(
            character in settings for character in ("\n", "\r", "\x00")
        ):
            raise ValueError("A cue cannot contain blank text lines or invalid placement settings.")
        seen.append(dict(start=start, end=end, text=text, settings=settings.strip()))
    return sorted(seen, key=lambda item: item["start"])


def parse(content, format):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Save the captions as UTF-8 and upload them again.") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n[ \t]*\n", text)
    if format == "vtt":
        if not re.match(r"^WEBVTT(?:[ \t][^\n]*)?(?:\n|$)", blocks[0]):
            raise ValueError("A WebVTT file must start with WEBVTT.")
        blocks = blocks[1:]
    result = []
    for block in blocks:
        if format == "vtt" and re.match(r"^(NOTE(?:[ \t]|$)|STYLE$|REGION$)", block.split("\n", 1)[0]):
            continue
        lines = block.split("\n")
        index = 0 if "-->" in lines[0] else 1
        match = TIMING.fullmatch(lines[index]) if len(lines) > index else None
        if match is None:
            raise ValueError("A subtitle cue is missing its start and end times.")
        start, end, options = match.groups()
        result.append(dict(start=timestamp(start), end=timestamp(end), text="\n".join(lines[index + 1:]),
                           settings=options or ""))
    return validate(result)


def serialize(cues):
    lines = ["WEBVTT", ""]
    for item in validate(cues):
        timing = formatted(item["start"]) + " --> " + formatted(item["end"])
        lines.extend([timing + (" " + item["settings"] if item["settings"] else ""), item["text"], ""])
    return ("\n".join(lines) + "\n").encode("utf-8")
