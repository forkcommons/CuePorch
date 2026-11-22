"""Read-only Vimeo API transport and private, checkpointed collection migration."""

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPException
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import event_schedule
from cueporch import checksum, dump, verify, work_id
from vimeo_appearance import custom_logo, embed_appearance, embed_ending, watch_appearance


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class SourceFailure(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(
            f"Source returned HTTP {status}; resume to refresh source links"
        )


class MediaFailure(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


INTERRUPTED = (URLError, HTTPException, ConnectionError, TimeoutError)


class Source:
    def __init__(self, simulation=None, token=None):
        self.base = simulation or "https://api.vimeo.com"
        self.simulation = simulation is not None
        if self.simulation:
            if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", self.base):
                raise ValueError("Simulation requires a loopback HTTP origin")
            self.token = "cueporch-fixture"
        else:
            self.token = token or os.environ.get("VIMEO_ACCESS_TOKEN")
            if not self.token:
                raise ValueError("Set VIMEO_ACCESS_TOKEN for the source account")
        self.opener = build_opener(NoRedirect())

    def open(self, url, authenticated=False, *, vimeo_image=False):
        for redirects in range(7):
            if vimeo_image:
                try:
                    parts = urlsplit(url)
                    port = parts.port
                except ValueError:
                    raise MediaFailure("unexpected_logo_origin") from None
                loopback = self.simulation and (parts.scheme, parts.netloc) == (
                    urlsplit(self.base).scheme,
                    urlsplit(self.base).netloc,
                )
                cdn = (
                    not self.simulation
                    and parts.scheme == "https"
                    and parts.hostname in {"i.vimeocdn.com", "f.vimeocdn.com"}
                    and port in (None, 443)
                )
                if (
                    authenticated
                    or not (loopback or cdn)
                    or parts.username is not None
                    or parts.password is not None
                    or parts.fragment
                ):
                    raise MediaFailure("unexpected_logo_origin")
            else:
                parts = urlsplit(url)
                same = (parts.scheme, parts.netloc) == (
                    urlsplit(self.base).scheme,
                    urlsplit(self.base).netloc,
                )
                if authenticated or self.simulation:
                    if not same:
                        raise ValueError("Unexpected source origin")
                elif parts.scheme != "https" or parts.username or parts.password:
                    raise ValueError("Invalid media URL")
            headers = {"Authorization": "Bearer " + self.token} if authenticated else {}
            for attempt in range(3):
                try:
                    return self.opener.open(Request(url, headers=headers), timeout=45)
                except HTTPError as exc:
                    if exc.code == 429 and attempt < 2:
                        delay = exc.headers.get("Retry-After", "2")
                        exc.close()
                        if not delay.isdigit() or int(delay) > 30:
                            raise SourceFailure(429) from None
                        time.sleep(int(delay))
                        continue
                    if not authenticated and exc.code in (301, 302, 303, 307, 308):
                        url = urljoin(url, exc.headers["Location"])
                        exc.close()
                        break
                    code = exc.code
                    exc.close()
                    raise SourceFailure(code) from None
        if vimeo_image:
            raise MediaFailure("logo_redirect_limit")
        raise ValueError("Media redirect limit exceeded")

    def pages(self, path, *, fields=None, params=None):
        original = dict(params) if params else {}
        query = {"per_page": 100}
        if fields:
            query["fields"] = fields
        query.update(original)
        url = self.base + path + "?" + urlencode(query)
        visited = set()
        count = 0
        total = None
        while url:
            parts = urlsplit(url)
            if parts.path != path:
                raise ValueError("Invalid source pagination")
            if original:
                pairs = parse_qsl(parts.query, keep_blank_values=True)
                modified = False
                for key, expected in original.items():
                    matches = [value for name, value in pairs if name == key]
                    if len(matches) > 1:
                        raise ValueError(f"duplicate query parameter: {key}")
                    if matches:
                        if matches[0] != expected:
                            raise ValueError(f"conflicting query parameter: {key}")
                    else:
                        pairs.append((key, expected))
                        modified = True
                if modified:
                    url = urlunsplit(parts._replace(query=urlencode(pairs)))
            if url in visited:
                raise ValueError("Invalid source pagination")
            visited.add(url)
            with self.open(url, True) as response:
                page = json.load(response)
            if isinstance(page, list):
                if len(visited) != 1:
                    raise ValueError("Inconsistent collection response")
                yield from page
                return
            if total is None:
                total = page["total"]
            if total != page["total"]:
                raise ValueError("Source changed during inventory")
            for row in page["data"]:
                count += 1
                yield row
            following = page["paging"]["next"]
            url = urljoin(url, following) if following else None
        if count != total:
            raise ValueError("Source count mismatch")


def vimeo_review_settings(row, video_uri):
    uri = row.get("uri")
    if not isinstance(uri, str) or not isinstance(video_uri, str):
        return None

    base = video_uri.rstrip("/")
    pat = re.compile(
        r"^" + re.escape(base) + r"/review_links/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.I,
    )
    if not pat.fullmatch(uri):
        return None

    raw_name = row.get("name")
    if not isinstance(raw_name, str):
        return None
    name = raw_name.strip()
    if not 1 <= len(name) <= 128:
        return None

    expires = None
    eo = row.get("expires_on")
    if isinstance(eo, str):
        try:
            dt = datetime.fromisoformat(eo.replace("Z", "+00:00") if eo.endswith("Z") else eo)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            expires = dt.timestamp()
        except (ValueError, OverflowError, OSError):
            pass

    pw = row.get("password")
    return {
        "id": hashlib.sha256(uri.lower().encode()).hexdigest(),
        "name": name,
        "enabled": row.get("is_enabled") is True,
        "needs_password": not ("password" in row and (pw is None or pw == "")),
        "logo_required": row.get("show_my_logo") is True,
        "allow_download": row.get("allow_downloads") is True,
        "allow_versions": row.get("show_all_versions") is True,
        "allow_comment": row.get("allow_comments") is True,
        "allow_status": row.get("allow_status_change") is True,
        "allow_resolved": row.get("show_resolved_comments") is True,
        "dark": row.get("is_dark") is True,
        "show_other_videos": row.get("hide_end_cards") is False,
        "expires": expires,
    }

def video_page(source, video):
    page = video.get('page')
    if isinstance(page, dict):
        return page

    url = source.base + video['uri'] + '?' + urlencode({'fields': 'uri,page'})

    try:
        with source.open(url, True) as response:
            payload = json.load(response)
    except SourceFailure as exc:
        if exc.status in (403, 404):
            return None
        raise

    if (
        isinstance(payload, dict)
        and payload.get('uri') == video['uri']
        and isinstance(payload.get('page'), dict)
    ):
        return payload['page']

    return None


def video_review_inventory(source, video_uri):
    link_fields = (
        "uri", "name", "is_enabled", "password", "expires_on",
        "allow_downloads", "show_all_versions", "allow_comments",
        "allow_status_change", "show_resolved_comments", "is_dark",
        "hide_end_cards", "show_my_logo",
    )
    fields = ",".join(("review_status", *(f"review_links.{field}" for field in link_fields)))
    url = source.base + video_uri + "?" + urlencode({"fields": fields})
    inventory = dict(state="unavailable", links=[], status=None)
    try:
        with source.open(url, True) as response:
            payload = json.load(response)
    except SourceFailure as exc:
        if exc.status not in (403, 404):
            raise
        return inventory

    if not isinstance(payload, dict):
        return inventory
    status = payload.get("review_status")
    if status in ("in_progress", "needs_review", "approved"):
        inventory["status"] = status
    rows = payload.get("review_links")
    if not isinstance(rows, list):
        return inventory
    by_id = {}
    for row in rows:
        if isinstance(row, dict) and (settings := vimeo_review_settings(row, video_uri)):
            by_id.setdefault(settings["id"], settings)
    return inventory | dict(state="available", links=list(by_id.values()))


def vimeo_viewer_comment(row, endpoint):
    if not isinstance(row, dict):
        return None
    uri = row.get('uri')
    if not isinstance(uri, str) or not re.fullmatch(re.escape(endpoint) + r'/[0-9]+', uri):
        return None
    if row.get('type', 'video') != 'video' or row.get('deleted_on'):
        return None
    text = row.get('text')
    if not isinstance(text, str) or not (1 <= len(text) <= 12000) or '\x00' in text:
        return None
    created_on = row.get('created_on')
    if not isinstance(created_on, str):
        return None
    try:
        dt = datetime.fromisoformat(created_on.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        created = dt.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    author = 'Vimeo viewer'
    metadata = row.get('metadata')
    if isinstance(metadata, dict):
        connections = metadata.get('connections')
        if isinstance(connections, dict):
            for key in ('user', 'guest_user'):
                node = connections.get(key)
                if isinstance(node, dict):
                    name = node.get('name')
                    if isinstance(name, str) and name.strip():
                        author = name.strip()[:256]
                        break
    legacy_user = row.get("user")
    if author == "Vimeo viewer" and isinstance(legacy_user, dict):
        name = legacy_user.get("name")
        if isinstance(name, str) and name.strip():
            author = name.strip()[:256]
    return {'id': hashlib.sha256(uri.encode()).hexdigest()[:32], 'author': author, 'body': text, 'created': created}


def viewer_comment_inventory(source, video_uri):
    fields = 'uri,type,text,created_on,deleted_on,metadata.connections.user.name,metadata.connections.guest_user.name'

    def read(path, *, include_reply_count=False):
        requested = fields
        if include_reply_count:
            requested += ',user.name,metadata.connections.replies.total'
        try:
            items = list(source.pages(path, fields=requested))
        except SourceFailure as exc:
            if exc.status in (403, 404):
                return None
            raise
        seen, out = set(), []
        for item in items:
            comment = vimeo_viewer_comment(item, path)
            if comment is None:
                continue
            key = comment["id"]
            if key in seen:
                continue
            seen.add(key)
            out.append((item, comment))
        return out
    roots = read(video_uri + '/comments', include_reply_count=True)
    if roots is None:
        return dict(state="unavailable", roots=[])
    inventory = []
    for raw, root in roots:
        data = raw
        for key in ("metadata", "connections", "replies", "total"):
            data = data.get(key) if isinstance(data, dict) else None
        total = data
        if type(total) is int and total == 0:
            replies = []
        else:
            replies = read(raw["uri"] + "/replies")
        inventory.append(root | dict(replies=[item for _, item in replies or []], replies_available=replies is not None))
    return dict(state="available", roots=inventory)


def assets(video, tracks):
    candidates = ((video.get("download") or []) + (video.get("files") or [])
                  + ((video.get("play") or {}).get("progressive") or []))
    downloads = [
        f
        for f in candidates
        if f.get("link")
        and f.get("quality") not in ("hls", "audio")
        and f.get("rendition") not in ("adaptive", "audio")
        and (f.get("type") or "").lower() not in (
            "application/x-mpegurl", "application/vnd.apple.mpegurl", "application/dash+xml", "audio/mp4",
        )
    ]
    if downloads:
        media = max(
            downloads,
            key=lambda f: (
                f.get("quality") in ("source", "original")
                or f.get("rendition") == "source",
                f.get("height") or 0,
                f.get("size") or 0,
            ),
        )
        original = (
            media.get("quality") in ("source", "original")
            or media.get("rendition") == "source"
        )
        fmt = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}.get(
            media.get("type"), "bin"
        )
        yield (
            media["link"],
            dict(role="original" if original else "playback", format=fmt),
            media.get("size"),
            media.get("md5") or None,
        )
    for track in tracks:
        if not track.get("link"):
            raise ValueError("Caption track has no accessible download link")
        yield (
            track["link"],
            dict(
                role="caption",
                format="vtt",
                language=track.get("language"),
                provenance="Vimeo text track",
                text_kind=track.get("type"),
                source_track=track["uri"],
            ),
            None,
            None,
        )
    pictures = [
        p for p in (video.get("pictures") or {}).get("sizes", []) if p.get("link")
    ]
    if pictures:
        yield (
            max(pictures, key=lambda p: p["width"])["link"],
            dict(role="poster", format="image"),
            None,
            None,
        )


def chapter_picture_items(chapters, related, video_uri):
    if isinstance(chapters, dict):
        chapter_iter = chapters.values()
    else:
        chapter_iter = chapters or []
    chapter_re = re.compile(re.escape(video_uri) + r'/chapters/[0-9]+')
    for chapter in chapter_iter:
        if not isinstance(chapter, dict):
            continue
        uri = chapter.get('uri')
        if not isinstance(uri, str) or not chapter_re.fullmatch(uri):
            continue
        pictures = related(uri.removeprefix(video_uri) + '/pictures')
        if not pictures:
            continue
        pic_re = re.compile(re.escape(uri) + r'/pictures/[A-Za-z0-9_-]+')
        chosen = None
        for picture in pictures:
            if not isinstance(picture, dict):
                continue
            if picture.get('active') is not True:
                continue
            puri = picture.get('uri')
            if not isinstance(puri, str) or not pic_re.fullmatch(puri):
                continue
            best_w = -1
            best_link = None
            for size in picture.get('sizes') or []:
                if not isinstance(size, dict):
                    continue
                w = size.get('width')
                if type(w) is not int or w <= 0 or w > 1280:
                    continue
                link = size.get('link')
                if not isinstance(link, str) or not link:
                    continue
                if w > best_w:
                    best_w = w
                    best_link = link
            if best_link is not None:
                chosen = (best_link, puri)
                break
        if chosen is None:
            continue
        yield (chosen[0], dict(role='chapter_picture', format='image', chapter_uri=uri, source_picture=chosen[1]), None, None)


def store(root, source, link, expected, source_md5, *, max_bytes=None, check_image=False, vimeo_image=False):
    digest = hashlib.sha256()
    if source_md5 and (not isinstance(source_md5, str) or not re.fullmatch("[a-fA-F0-9]{32}", source_md5)):
        raise MediaFailure("invalid_source_checksum")
    source_digest = hashlib.md5(usedforsecurity=False) if source_md5 else None
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as target:
        temporary = Path(target.name)
    try:
        try:
            response = source.open(link, vimeo_image=True) if vimeo_image else source.open(link)
        except INTERRUPTED:
            raise MediaFailure("media_connection_interrupted") from None
        with response, temporary.open("wb") as target:
            announced = response.headers.get("Content-Length")
            if max_bytes is not None and announced is not None and str(announced).isdigit() and int(announced) > max_bytes:
                raise MediaFailure("image_too_large")
            while True:
                try:
                    block = response.read(1024 * 1024)
                except INTERRUPTED:
                    raise MediaFailure("media_connection_interrupted") from None
                if not block:
                    break
                if max_bytes is not None and target.tell() + len(block) > max_bytes:
                    raise MediaFailure("image_too_large")
                target.write(block)
                digest.update(block)
                if source_digest:
                    source_digest.update(block)
        size = temporary.stat().st_size
        # The API size is approximate. Prefer the HTTP size and the supplied file digest.
        length = announced if announced is not None else (None if source_digest else expected)
        if not size or (length is not None and (not str(length).isdigit() or int(length) != size)):
            raise MediaFailure("incomplete_media")
        if source_digest and source_digest.hexdigest() != source_md5.lower():
            raise MediaFailure("source_checksum_mismatch")
        if check_image:
            from library_thumbnail import thumbnail_type
            if not thumbnail_type(temporary):
                raise MediaFailure("invalid_image_type")
        key = digest.hexdigest()
        temporary.replace(root / "objects" / key)
        return dict(sha256=key, bytes=size)
    finally:
        temporary.unlink(missing_ok=True)


class RefreshLinks(Exception):
    pass


def canonical_version_uri(video_uri, value):
    if isinstance(value, str) and re.fullmatch(re.escape(video_uri) + r"/versions/[0-9]+", value):
        return value
    return None


def version_asset(version):
    playable = folder_field(version, "play", "progressive")
    if not isinstance(playable, list):
        return None
    videos = [
        item for item in playable if isinstance(item, dict)
        and item.get("type") in ("video/mp4", "video/webm") and isinstance(item.get("link"), str)
    ]
    return next(assets({"play": {"progressive": videos}}, ()), None)


def version_declines_playback(version):
    return (
        version.get("is_deleted") is True or version.get("is_playable") is False
        or folder_field(version, "play", "status") not in (None, "playable")
    )


def archive_version(source, video_uri, row):
    uri = canonical_version_uri(video_uri, row.get("uri"))
    if uri is None:
        return row, None, "Unrecognized source version identity"
    if row.get("active") is not False:
        return row, None, "Vimeo did not identify this as a previous cut"
    if version_declines_playback(row):
        return row, None, "Vimeo did not allow playback"
    asset = version_asset(row)
    if asset is not None:
        return row, asset, None
    try:
        with source.open(source.base + uri, authenticated=True) as response:
            detail = json.load(response)
    except SourceFailure as error:
        if error.status in (403, 404, 410):
            return row, None, f"Source version unavailable (HTTP {error.status})"
        raise
    if not isinstance(detail, dict) or detail.get("uri") != uri or detail.get("active") is not False:
        return row, None, "Vimeo did not confirm the previous cut's identity"
    if version_declines_playback(detail):
        return detail, None, "Vimeo did not allow playback"
    asset = version_asset(detail)
    return detail, asset, None if asset else "Vimeo supplied no supported progressive file"


def archived_version_files(root, raw):
    if raw["format"] == "mp4":
        return [raw]
    from media_tools import prepare_screening

    with tempfile.TemporaryDirectory(dir=root) as temp:
        screening = Path(temp) / "screening.mp4"
        try:
            prepare_screening(root / "objects" / raw["sha256"], screening,
                              input_format="matroska", full_resolution=True)
        except (OSError, ValueError):
            raise MediaFailure("Could not prepare an MP4 screening copy") from None
        digest, size = checksum(screening), screening.stat().st_size
        screening.replace(root / "objects" / digest)
    return [dict(role="playback", format="mp4", sha256=digest, bytes=size), raw]


def verified_version_objects(root, files):
    if not isinstance(files, list) or not files:
        return False
    for file in files:
        digest = file["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
        path = root / "objects" / digest
        if path.stat().st_size != file["bytes"] or checksum(path) != digest:
            return False
    return True


def recover_version(root, source, uri, detail, asset, *, refresh):
    link, meta, size, md5 = asset
    revision = detail.get("modified_time") or detail.get("created_time")
    scope = dict(uri=uri, revision=revision, media=meta, size=size, md5=md5)
    checkpoint = root / "checkpoints" / (uri.strip("/").replace("/", "-") + ".json")
    if revision or md5:
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf8"))
            files = saved["files"]
            if saved["scope"] == scope and verified_version_objects(root, files):
                return files, None
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        raw = dict(meta, **store(root, source, link, size, md5))
        files = archived_version_files(root, raw)
    except MediaFailure as error:
        return None, error.reason
    except SourceFailure as error:
        if error.status == 429:
            raise
        if refresh and error.status in (401, 403, 410):
            raise RefreshLinks() from None
        return None, f"Source media unavailable (HTTP {error.status})"
    if revision or md5:
        dump(checkpoint, dict(scope=scope, files=files))
    return files, None


def retain_previous_versions(root, source, video_uri, title, versions, *, dry_run, refresh):
    history, recovery = [], []
    seen = set()
    for row in versions:
        if row.get("active") is True:
            continue
        uri = canonical_version_uri(video_uri, row.get("uri"))
        if uri is not None:
            if uri in seen:
                continue
            seen.add(uri)
        detail, asset, issue = archive_version(source, video_uri, row)
        number = detail.get("sequence_number")
        entry = dict(uri=uri, number=number if type(number) is int else None)
        if asset is not None and dry_run:
            entry["state"] = "available"
        else:
            files = None
            if asset is not None:
                files, issue = recover_version(root, source, uri, detail, asset, refresh=refresh)
            if files:
                entry["state"] = "recovered"
                saved_at = detail.get("upload_date") or detail.get("created_time")
                history.append(dict(
                    files=files, title=title, version_saved_at=saved_at if isinstance(saved_at, str) else None,
                    duration_seconds=detail.get("duration"), disposition="playback copy only",
                    source_version=uri, source_version_number=entry["number"],
                ))
            else:
                entry.update(state="owner_file_needed", issue=issue)
        recovery.append(entry)
    history.sort(key=lambda version: version["version_saved_at"] or "")
    return history, recovery


def alternate_audio_asset(row, version_uri):
    from library_audio import FORMATS
    from subtitles import LANGUAGE

    uri = folder_field(row, "uri")
    if not isinstance(uri, str) or not re.fullmatch(re.escape(version_uri) + r"/audiotracks/[A-Za-z0-9-]+", uri):
        return None
    code = folder_field(row, "language", "code")
    language = code.lower() if isinstance(code, str) and LANGUAGE.fullmatch(code) else None
    audio_type = row.get("type")
    kinds = {"main": "dubbed", "descriptive": "description", "commentary": "commentary"}
    kind = kinds.get(audio_type) if isinstance(audio_type, str) else None
    report = dict(version_uri=version_uri, uri=uri, language=language, kind=kind, state="owner_file_needed")
    if row.get("active") is False:
        report["state"] = "disabled"
        return report, None
    if not language or not kind or row.get("active") is not True:
        return report, None
    downloads = row.get("download")
    if folder_field(row, "transcode", "status") == "complete" and isinstance(downloads, list):
        for item in downloads:
            extension = folder_field(item, "extension")
            if (isinstance(extension, str) and extension in FORMATS and isinstance(item.get("link"), str)
                    and item["link"] and item.get("rendition") == "source"):
                report["state"] = "available"
                return report, (item["link"], extension)
    return report, None


def vimeo_audio_tracks(source, version_uri):
    try:
        rows = list(source.pages(version_uri + "/audiotracks"))
    except SourceFailure as error:
        if error.status not in (403, 404, 410):
            raise
        yield dict(version_uri=version_uri, state="inventory_unavailable", issue=f"HTTP {error.status}"), None
        return
    seen = set()
    for row in rows:
        item = alternate_audio_asset(row, version_uri)
        if item is None:
            continue
        report, asset = item
        if report["uri"] not in seen:
            seen.add(report["uri"])
            yield report, asset


def recover_alternate_audio(root, source, playback, asset, *, refresh):
    from subprocess import TimeoutExpired

    import library_audio

    link, format = asset
    try:
        raw = dict(store(root, source, link, None, None), format=format)
    except SourceFailure as error:
        if error.status == 429:
            raise
        if refresh and error.status in (401, 403, 410):
            raise RefreshLinks() from None
        return None, f"Source audio unavailable (HTTP {error.status})"
    except ValueError:
        return None, "Could not download the source audio"

    scope = f"{playback['sha256']}:{raw['sha256']}:{format}"
    key = hashlib.sha256(scope.encode()).hexdigest()
    checkpoint = root / "checkpoints" / f"audio-{key}.json"
    try:
        rendition = json.loads(checkpoint.read_text(encoding="utf8"))
        if verified_version_objects(root, [rendition]):
            return dict(source=raw, rendition=rendition), None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    with tempfile.TemporaryDirectory(dir=root) as temp:
        target = Path(temp) / "audio.mp4"
        try:
            library_audio.render_audio(root / "objects" / playback["sha256"],
                                       root / "objects" / raw["sha256"],
                                       target, library_audio.FORMATS[format])
            rendition = library_audio.object_info(target, "mp4")
            library_audio.store(root, target, rendition)
        except (OSError, ValueError, TimeoutExpired):
            return None, "Could not prepare alternate audio"
    dump(checkpoint, rendition)
    return dict(source=raw, rendition=rendition), None


def attach_version_audio(root, source, record, versions, *, dry_run, refresh):
    from catalog import video_version_key
    from library_privacy import playback

    cuts = {cut.get("source_version"): cut for cut in [record, *record.get("versions_history", [])]}
    video_uri = "/videos/" + record["source_id"]
    version_uris = dict.fromkeys(canonical_version_uri(video_uri, version.get("uri")) for version in versions)
    reports = []
    for uri in version_uris:
        if uri is None:
            continue
        cut = cuts.get(uri)
        for report, asset in vimeo_audio_tracks(source, uri):
            reports.append(report)
            if dry_run or asset is None:
                continue
            selected = playback(cut) if cut else None
            if selected is None:
                report.update(state="owner_file_needed", issue="This cut needs a playback file first")
                continue
            key = video_version_key(cut)
            edit = record.setdefault("language_edits", {}).setdefault(key, {})
            slot = [report["language"], report["kind"]]
            if slot in edit.get("vimeo_audio_slots", []):
                report.update(state="owner_file_needed", issue="This cut already has audio with that language and type")
                continue
            parts, issue = recover_alternate_audio(root, source, selected, asset, refresh=refresh)
            if parts is None:
                report.update(state="owner_file_needed", issue=issue)
                continue
            edit.setdefault("audio", []).append(dict(parts, language=slot[0], kind=slot[1]))
            edit.setdefault("vimeo_audio_slots", []).append(slot)
            report["state"] = "recovered"
    if reports:
        record["audio_recovery"] = reports


def previous_text_track(row, version_uri):
    from subtitles import LANGUAGE
    if not isinstance(row, dict) or not isinstance(version_uri, str):
        return None
    m = re.fullmatch(r"/videos/(\d+)/versions/(\d+)", version_uri)
    if not m:
        return None
    vid, ver = m.groups()
    uri = row.get("uri")
    if not isinstance(uri, str):
        return None
    m = re.fullmatch(r"/videos/(\d+)/versions/(\d+)/texttracks/(\d+)", uri)
    if m:
        if (m.group(1), m.group(2)) != (vid, ver):
            return None
    else:
        m = re.fullmatch(r"/videos/(\d+)/texttracks/(\d+)", uri)
        if not m or m.group(1) != vid:
            return None
    language = folder_field(row, "language")
    kind = folder_field(row, "type")
    report = {"uri": uri, "language": language, "kind": kind}
    if not isinstance(language, str) or not LANGUAGE.fullmatch(language):
        report["state"] = "owner_file_needed"
        return report, None
    language = language.lower()
    report["language"] = language
    if kind not in ("captions", "subtitles"):
        report["state"] = "owner_file_needed"
        return report, None
    active = row.get("active")
    if active is False:
        report["state"] = "disabled"
        return report, None
    if active is not True:
        report["state"] = "owner_file_needed"
        return report, None
    vtt = folder_field(row, "download_links", "vtt")
    if isinstance(vtt, str) and vtt.strip():
        report["state"] = "available"
        return report, (vtt.strip(), "vtt")
    link = folder_field(row, "link")
    if isinstance(link, str) and link.strip():
        report["state"] = "available"
        return report, (link.strip(), "vtt")
    srt = folder_field(row, "download_links", "srt")
    if isinstance(srt, str) and srt:
        report["state"] = "available"
        return report, (srt.strip(), "srt")
    report["state"] = "owner_file_needed"
    return report, None


def previous_text_tracks(source, uri):
    try:
        rows = list(source.pages(uri + "/texttracks", fields="uri,language,type,active,link,download_links"))
    except SourceFailure as error:
        if error.status not in (403, 404, 410):
            raise
        yield dict(state="inventory_unavailable", issue=f"HTTP {error.status}"), None
        return
    seen = set()
    for row in rows:
        item = previous_text_track(row, uri)
        if item is None:
            continue
        report, asset = item
        if report["uri"] not in seen:
            seen.add(report["uri"])
            yield report, asset


def recover_previous_text_track(root, source, asset, *, refresh):
    import subtitles
    from library_languages import save_blob

    link, format = asset
    limit = 8 * 1024 * 1024

    try:
        with source.open(link) as response:
            content = response.read(limit + 1)
    except SourceFailure as exc:
        status = exc.status
        if status == 429:
            raise
        if refresh and status in (401, 403, 410):
            raise RefreshLinks() from None
        return (None, f"HTTP {status}")
    except ValueError:
        return (None, "source unavailable")

    if len(content) > limit:
        return (None, "text track too large")

    try:
        parsed = subtitles.parse(content, format)
        if format.lower() == "srt":
            content = subtitles.serialize(parsed)
    except ValueError:
        return (None, "invalid subtitle content")

    return (save_blob(root, content), None)


def attach_previous_text_tracks(root, source, record, *, dry_run, refresh):
    from catalog import video_version_key

    cuts = {cut["source_version"]: cut for cut in record.get("versions_history", [])}

    for entry in record.get("version_recovery", []):
        uri = entry["uri"]
        if uri is None:
            continue

        for report, asset in previous_text_tracks(source, uri):
            entry.setdefault("text_tracks", []).append(report)

            if dry_run or asset is None:
                continue

            cut = cuts.get(uri)
            if cut is None:
                report["state"] = "owner_file_needed"
                continue

            blob, issue = recover_previous_text_track(root, source, asset, refresh=refresh)
            if blob is None:
                report.update(state="owner_file_needed", issue=issue)
                continue

            edits = record.setdefault("language_edits", {}).setdefault(
                video_version_key(cut), {}
            )
            edits.setdefault("tracks", []).append(
                dict(
                    blob,
                    role="caption",
                    format="vtt",
                    language=report["language"],
                    text_kind=report["kind"],
                    source_track=report["uri"],
                )
            )
            edits.setdefault("vimeo_text_tracks", []).append(report["uri"])
            report["state"] = "recovered"


def original_url(video, vid):
    fallback = "https://vimeo.com/" + vid
    link = video.get("link")
    if not isinstance(link, str):
        return fallback
    try:
        parsed = urlsplit(link)
    except ValueError:
        return fallback
    if (
        parsed.scheme == "https" and parsed.netloc in {"vimeo.com", "www.vimeo.com"}
        and re.fullmatch(r"/" + vid + r"(?:/[a-zA-Z0-9]+)?/?", parsed.path)
    ):
        return "https://vimeo.com" + parsed.path.rstrip("/")
    return fallback


def folder_field(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def canonical_folder_uri(uri, account):
    if not isinstance(uri, str):
        return None
    match = re.fullmatch(r"(?:/users/([0-9]+)|/me)/(?:projects|folders)/([0-9]+)", uri)
    if not match or (match[1] and account != "/users/" + match[1]):
        return None
    return f"{account}/projects/{match[2]}"


def folder_node(project, parent):
    name = folder_field(project, "name")
    if not isinstance(name, str) or not name.strip():
        return None
    color = folder_field(project, "settings", "color")
    if isinstance(color, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
        color = "#" + color.removeprefix("#")
    else:
        color = "#5678a1"
    return dict(name=name.strip(), parent=parent, color=color)


def add_folder(project, account, nodes, *, catalog=False):
    uri = canonical_folder_uri(folder_field(project, "uri"), account)
    if uri is None:
        return None
    connections = folder_field(project, "metadata", "connections")
    path = folder_field(connections, "ancestor_path")
    ancestors = []
    if isinstance(path, list):
        ancestors = [canonical_folder_uri(folder_field(item, "uri"), account) for item in path]
        if all(ancestors):
            parent = None
            for item, ancestor in reversed(list(zip(path, ancestors))):
                if node := folder_node(item, parent):
                    nodes.setdefault(ancestor, node)
                parent = ancestor
    if ancestors and all(ancestors):
        parent = ancestors[0]
    elif isinstance(connections, dict) and "parent_folder" in connections:
        reference = connections["parent_folder"]
        parent = None if reference is None else (
            canonical_folder_uri(folder_field(reference, "uri"), account) or ""
        )
    elif ancestors:
        parent = ""
    elif isinstance(path, list) or catalog:
        parent = None
    else:
        # A video may return only a folder name and URI. These do not establish its parent.
        return uri
    node = folder_node(project, parent)
    if node is not None:
        if catalog:
            nodes[uri] = node
        else:
            nodes.setdefault(uri, node)
    return uri


def order_folders(institution, nodes):
    children = defaultdict(list)
    queue = deque()
    for uri, node in nodes.items():
        parent = node["parent"]
        if parent is None:
            queue.append(uri)
        else:
            children[parent].append(uri)
    ordered = []
    resolved = {}
    while queue:
        uri = queue.popleft()
        node = nodes[uri]
        parent = resolved.get(node["parent"])
        item = dict(
            id=work_id(institution, "vimeo-folder", uri),
            parent=parent["id"] if parent else None,
            name=node["name"],
            color=node["color"],
            path=f"{parent['path']} / {node['name']}" if parent else node["name"],
        )
        ordered.append(item)
        resolved[uri] = item
        queue.extend(children[uri])
    return ordered, resolved


def folder_inventory(institution, source, account, profile, videos):
    uri = folder_field(profile, "metadata", "connections", "folders", "uri")
    default = account + "/projects"
    path = uri if uri in (default, account + "/folders", "/me/projects", "/me/folders") else default
    warnings = []
    nodes = {}
    try:
        projects = list(source.pages(path))
        incomplete = False
        for project in projects:
            if add_folder(project, account, nodes, catalog=True) not in nodes:
                incomplete = True
        if incomplete:
            warnings.append("Vimeo returned a folder without a usable identity or name.")
    except SourceFailure as error:
        if error.status not in (403, 404):
            raise
        warnings.append(f"Vimeo's folder listing was unavailable (HTTP {error.status}).")
    placements = {
        video["uri"]: add_folder(video["parent_folder"], account, nodes)
        for video in videos if video.get("parent_folder") is not None
    }
    ordered, resolved = order_folders(institution, nodes)
    by_video = {video: resolved.get(folder) for video, folder in placements.items()}
    if nodes.keys() - resolved.keys() or any(folder is None for folder in by_video.values()):
        warnings.append("Some source folders or placements could not be resolved.")
    return ordered, by_video, " ".join(dict.fromkeys(warnings)) or None


def showcase_style(album):
    style = {}
    theme = album.get("theme")
    if theme in ("dark", "standard"):
        style["dark"] = theme == "dark"

    layout = album.get("layout")
    if layout in ("grid", "player"):
        style["layout"] = "featured" if layout == "player" else "grid"

    web_brand, embed_brand = album.get("web_brand_color"), album.get("embed_brand_color")
    if (web_brand is True or embed_brand is True) and not (web_brand is False or embed_brand is False):
        color = album.get("brand_color")
        if isinstance(color, str) and (match := re.fullmatch(r"#?([0-9a-fA-F]{6})", color.strip())):
            style["accent"] = "#" + match.group(1).lower()

    for source, target in (
        ("autoplay", "next_video"),
        ("loop", "loop"),
        ("allow_share", "copy_link"),
        ("allow_downloads", "download"),
    ):
        value = album.get(source)
        if type(value) is bool:
            style[target] = value
    if type(hide_nav := album.get("hide_nav")) is bool:
        style["navigation"] = not hide_nav

    for key, limit in (("seo_title", 140), ("seo_description", 200), ("seo_keywords", 500)):
        value = album.get(key)
        if isinstance(value, str) and value.strip():
            style[key] = value.strip()[:limit]
    return style


def transfer_showcase_logo(root, source, item, dry_run, issues):
    web, embed = item.get("web_custom_logo"), item.get("embed_custom_logo")
    if type(web) is bool and type(embed) is bool and web != embed:
        issues.append("The Vimeo web and embed logo preferences differ. Choose a logo in CuePorch.")
    if web is not True or embed is not True:
        return {}

    logo = item.get("custom_logo")
    sizes = logo.get("sizes") if isinstance(logo, dict) else None
    chosen = None
    for size in sizes if isinstance(sizes, list) else []:
        if not isinstance(size, dict):
            continue
        width, height, link = size.get("width"), size.get("height"), size.get("link")
        if (type(width) is not int or type(height) is not int or not 1 <= width <= 1280
                or not 1 <= height <= 1280 or not isinstance(link, str) or not link):
            continue
        if chosen is None or width > chosen["width"]:
            chosen = size
    if chosen is None:
        issues.append("The Vimeo showcase logo did not include a usable image.")
        return {}
    if dry_run:
        return dict(source_logo_available=True)

    try:
        return dict(source_logo=store(root, source, chosen["link"], None, None,
                                      max_bytes=4 * 1024 * 1024, check_image=True, vimeo_image=True))
    except SourceFailure as exc:
        if exc.status not in (403, 404):
            raise
        issues.append(f"Showcase logo returned HTTP {exc.status}.")
    except MediaFailure as exc:
        issues.append(f"Showcase logo unavailable ({exc.reason}).")
    return {}


def showcase_inventory(institution, source, account, video_ids, *, root, dry_run):
    path = account + "/albums"
    try:
        albums = list(source.pages(path))
    except SourceFailure as exc:
        if exc.status in (403, 404):
            return [], f"Showcase inventory returned HTTP {exc.status}. Check the account's showcase access."
        raise
    result = []
    seen = set()
    for item in albums:
        identity = re.fullmatch(r"(?:/users/[0-9]+)?/albums/([0-9]+)", item.get("uri", ""))
        if not identity:
            raise ValueError("Invalid showcase identity")
        identifier = identity.group(1)
        if identifier in seen:
            raise ValueError("Duplicate showcase identity")
        seen.add(identifier)
        canonical = path + "/" + identifier
        issues = []
        try:
            members = list(source.pages(canonical + "/videos"))
        except SourceFailure as exc:
            if exc.status not in (403, 404):
                raise
            members = []
            issues.append(f"Video list returned HTTP {exc.status}.")
        ids = []
        for member in members:
            uri = re.fullmatch(r"/videos/([0-9]+)", member.get("uri", ""))
            if not uri:
                raise ValueError("Invalid showcase member identity")
            ids.append(uri.group(1))
        missing = [vid for vid in ids if vid not in video_ids]
        recovered = [work_id(institution, "vimeo", vid) for vid in ids if vid in video_ids]
        visibility = item.get("privacy", "")
        if isinstance(visibility, dict):
            visibility = visibility.get("view", "")
        if visibility not in ("anybody", "public", "nobody", "private", "password", "embed_only", "team", "unlisted"):
            visibility = "unknown"
        web_brand, embed_brand = item.get("web_brand_color"), item.get("embed_brand_color")
        if type(web_brand) is bool and type(embed_brand) is bool and web_brand != embed_brand:
            issues.append("The Vimeo web and embed accent preferences differ. Choose the accent in CuePorch.")
        title, description = item.get("name"), item.get("description")
        logo = transfer_showcase_logo(root, source, item, dry_run, issues)
        result.append(dict(id=work_id(institution, "vimeo-showcase", canonical),
                           uri=canonical, source_id=identifier, source_url=f"https://vimeo.com/showcase/{identifier}",
                           title=(title.strip()[:128] if isinstance(title, str) else "") or "Untitled showcase",
                           description=description.strip()[:5000] if isinstance(description, str) else "",
                           source_visibility=visibility, source_style=showcase_style(item),
                           members=list(dict.fromkeys(recovered)),
                           missing=list(dict.fromkeys(missing)), issues=issues, **logo))
    return result, None


def channel_inventory(institution, source, account, video_ids):
    path = account + "/channels"
    try:
        channels = list(source.pages(path, params={"filter": "moderated"}))
    except SourceFailure as exc:
        if exc.status in (403, 404):
            return [], f"Channel inventory returned HTTP {exc.status}. Check the account's channel access."
        raise
    result = []
    seen = set()
    for item in channels:
        identity = re.fullmatch(r"/channels/([0-9]+)", item.get("uri", ""))
        if not identity:
            raise ValueError("Invalid channel identity")
        identifier = identity.group(1)
        if identifier in seen:
            raise ValueError("Duplicate channel identity")
        seen.add(identifier)
        uri = "/channels/" + identifier
        issues = []
        try:
            videos = list(source.pages(uri + "/videos"))
        except SourceFailure as exc:
            if exc.status not in (403, 404):
                raise
            videos = []
            issues.append(f"Video list returned HTTP {exc.status}.")
        ids = []
        for member in videos:
            member_uri = re.fullmatch(r"/videos/([0-9]+)", member.get("uri", ""))
            if not member_uri:
                raise ValueError("Invalid channel video identity")
            ids.append(member_uri.group(1))
        missing = list(dict.fromkeys(vid for vid in ids if vid not in video_ids))
        recovered = [work_id(institution, "vimeo", vid) for vid in ids if vid in video_ids]
        visibility = item.get("privacy", "")
        if isinstance(visibility, dict):
            visibility = visibility.get("view", "")
        if visibility not in ("anybody", "public", "nobody", "private", "password",
                              "embed_only", "team", "unlisted"):
            visibility = "unknown"
        source_category = ""
        categories = item.get("categories", [])
        if isinstance(categories, list):
            for cat in categories:
                if isinstance(cat, dict):
                    name = cat.get("name")
                    if isinstance(name, str) and name:
                        source_category = name.strip()[:120]
                        break
        source_url = "https://vimeo.com" + uri
        link = item.get("link")
        if isinstance(link, str):
            try:
                parts = urlsplit(link)
                if (parts.scheme == "https"
                        and parts.netloc in ("vimeo.com", "www.vimeo.com")
                        and not parts.query and not parts.fragment
                        and not parts.username and not parts.password
                        and re.fullmatch(r"/channels/[A-Za-z0-9_-]{1,80}", parts.path or "")):
                    source_url = link
            except (ValueError, AttributeError):
                pass
        title, description = item.get("name"), item.get("description")
        result.append(dict(
            id=work_id(institution, "vimeo-channel", uri),
            uri=uri,
            source_id=identifier,
            source_url=source_url,
            title=(title.strip()[:128] if isinstance(title, str) else "") or "Untitled channel",
            description=description.strip()[:5000] if isinstance(description, str) else "",
            source_visibility=visibility,
            source_category=source_category,
            kind="channel",
            members=list(dict.fromkeys(recovered)),
            missing=missing,
            issues=issues,
        ))
    return result, None


def event_schedule_draft(value):
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid event schedule")
    try:
        zone = ZoneInfo(value.get("time_zone", ""))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ValueError("Invalid event timezone") from None

    def local_minute(field):
        raw = value.get(field)
        if not isinstance(raw, str):
            raise ValueError("Missing event time")
        instant = datetime.fromisoformat(raw)
        if instant.tzinfo is None:
            raise ValueError("Missing event time offset")
        local = instant.astimezone(zone).replace(tzinfo=None)
        if local.second or local.microsecond or event_schedule.actual_time(local, zone) != instant.astimezone(timezone.utc):
            raise ValueError("Event time cannot be represented exactly")
        return local.isoformat(timespec="minutes")

    schedule = dict(timezone=zone.key, start=local_minute("start_time"), end=local_minute("end_time"))
    rule = value.get("rrule")
    if rule is not None and rule != "":
        if not isinstance(rule, str) or len(rule) > 500:
            raise ValueError("Invalid recurrence rule")
        parts = {}
        for part in rule.strip().upper().removeprefix("RRULE:").split(";"):
            name, sep, content = part.partition("=")
            if not sep or not content or name in parts or name not in {"FREQ", "INTERVAL", "BYDAY", "COUNT", "UNTIL"}:
                raise ValueError("Unsupported recurrence rule")
            parts[name] = content
        repeat = {"DAILY": "days", "WEEKLY": "weeks", "MONTHLY": "months"}.get(parts.get("FREQ"))
        if repeat is None:
            raise ValueError("Unsupported recurrence frequency")
        schedule["repeat"] = repeat
        for name in ("INTERVAL", "COUNT"):
            if name in parts:
                if not re.fullmatch(r"[1-9][0-9]{0,4}", parts[name]):
                    raise ValueError("Invalid recurrence number")
                schedule[name.lower()] = int(parts[name])
        if "BYDAY" in parts:
            names = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
            days = parts["BYDAY"].split(",")
            if repeat != "weeks" or any(day not in names for day in days):
                raise ValueError("Unsupported recurrence weekdays")
            schedule["weekdays"] = [names.index(day) for day in days]
        if "UNTIL" in parts:
            if "COUNT" in parts or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", parts["UNTIL"]):
                raise ValueError("invalid UNTIL")
            cutoff = datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            day = cutoff.astimezone(zone).date()
            opening = event_schedule.actual_time(datetime.combine(day, datetime.fromisoformat(schedule["start"]).time()), zone)
            if opening is not None and opening > cutoff:
                day -= timedelta(days=1)
            schedule["until"] = day.isoformat()
    return event_schedule.validate(schedule)


def _streamed_on_utc(raw):
    if not isinstance(raw, str) or len(raw) >= 70:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def vimeo_creator_profile(profile):
    name = profile.get("name")
    if not isinstance(name, str) or not name.strip() or re.search(r"[\x00-\x1f\x7f]", name):
        return None
    bio = profile.get("bio")
    if isinstance(bio, str):
        bio = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", "", bio.replace("\r\n", "\n")).strip()
    else:
        bio = ""

    links = []
    websites = profile.get("websites")
    for website in websites if isinstance(websites, list) else ():
        link = website.get("link") if isinstance(website, dict) else None
        if not isinstance(link, str):
            continue
        link = link.strip()
        if (not link or len(link) > 2048 or link in links
                or any(char.isspace() or char == "\\" or ord(char) < 32 or ord(char) == 127 for char in link)):
            continue
        try:
            parsed = urlsplit(link)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None or parsed.port == 0):
                continue
        except ValueError:
            continue
        links.append(link)
        if len(links) == 5:
            break
    return dict(name=name.strip()[:120], bio=bio[:2000], links=links)


def live_event_archive_inventory(institution, source, account, video_ids):
    fields = ("uri,title,stream_privacy.view,recordings.uri,recordings.title,recordings.streamed_on,"
              "schedule.start_time,schedule.end_time,schedule.time_zone,schedule.rrule,"
              "stream_description,preferred_stream_method,latency,"
              "chat_enabled,dvr,auto_cc_enabled,auto_cc_language,has_forced_lead_capture_form")
    try:
        events = list(source.pages(account + "/live_events", fields=fields))
    except SourceFailure as exc:
        if exc.status in (401, 403, 404):
            return [], f"Live event inventory returned HTTP {exc.status}. Check the account's Live API access."
        raise

    inventory = []
    event_ids = set()
    unresolved = False
    for event in events:
        uri = event.get("uri") if isinstance(event, dict) else None
        identity = re.fullmatch(r"/live_events/([0-9]+)", uri.removeprefix(account)) if isinstance(uri, str) else None
        if not identity or identity[1] in event_ids:
            unresolved = True
            continue
        identifier = identity[1]
        event_ids.add(identifier)
        title = event.get("title")
        privacy = event.get("stream_privacy")
        visibility = privacy.get("view") if isinstance(privacy, dict) else None
        if visibility not in ("anybody", "nobody", "password", "unlisted", "embed_only", "team"):
            visibility = "unknown"

        recordings = []
        seen = set()
        issues = []
        rows = event.get("recordings")
        if not isinstance(rows, list):
            issues.append("Vimeo did not return a usable recordings list.")
            rows = []
        invalid_recordings = False
        for recording in rows:
            uri = recording.get("uri") if isinstance(recording, dict) else None
            video = re.fullmatch(r"/videos/([0-9]+)", uri) if isinstance(uri, str) else None
            if not video:
                invalid_recordings = True
                continue
            vid = video[1]
            if vid in seen:
                continue
            seen.add(vid)
            name = recording.get("title")
            record = dict(source_id=vid, title=name.strip()[:128] if isinstance(name, str) else "",
                          work=work_id(institution, "vimeo", vid) if vid in video_ids else None)
            streamed_on = _streamed_on_utc(recording.get('streamed_on'))
            if streamed_on is not None:
                record['streamed_on'] = streamed_on
            recordings.append(record)
        if invalid_recordings:
            issues.append("Some recordings had no recognized Vimeo video ID.")
        draft = {}
        try:
            schedule = event_schedule_draft(event.get("schedule"))
        except (ValueError, OverflowError):
            issues.append("Recreate the Vimeo schedule in Live Events.")
        else:
            if schedule is not None:
                draft["schedule"] = schedule
                description = event.get("stream_description")
                if isinstance(description, str):
                    draft["description"] = description.strip()[:5000]
                method = event.get("preferred_stream_method")
                if method in ("browser", "encoder"):
                    draft["method"] = method
                latency = event.get("latency")
                if latency in ("standard", "low", "fail-safe"):
                    draft["latency"] = latency
                for source_field, target_field in (("chat_enabled", "chat_enabled"), ("dvr", "dvr_enabled")):
                    setting = event.get(source_field)
                    if type(setting) is bool:
                        draft[target_field] = setting
                enabled = event.get("auto_cc_enabled")
                language = event.get("auto_cc_language")
                if type(enabled) is bool:
                    if language in ("en", "de", "fr", "es", "it", "nl", "pl"):
                        draft["captions"] = dict(enabled=enabled, language=language)
                    elif not enabled:
                        draft["captions"] = dict(enabled=False)
                    else:
                        issues.append("Vimeo automatic captions use an unsupported or missing language. Set the caption language in Live Events.")
        if event.get("has_forced_lead_capture_form") is True:
            issues.append("Vimeo reports required registration. Recreate the form and its consent fields in Live Events.")
        inventory.append(dict(source_id=identifier, title=(title.strip()[:128] if isinstance(title, str) else "")
                              or "Untitled live event", source_visibility=visibility,
                              recordings=recordings, issues=issues, **draft))
    return inventory, "Some source events had missing or duplicate IDs." if unresolved else None


def resolve_archive_recordings(institution, source, account, events, videos, video_ids):
    unavailable = {}
    for event in events:
        for recording in event["recordings"]:
            vid = recording["source_id"]
            if vid not in video_ids and vid not in unavailable:
                uri = "/videos/" + vid
                try:
                    with source.open(source.base + uri, True) as response:
                        video = json.load(response)
                except SourceFailure as exc:
                    if exc.status not in (401, 403, 404):
                        raise
                    unavailable[vid] = f"Vimeo video detail returned HTTP {exc.status}."
                else:
                    owner = video.get("user") if isinstance(video, dict) else None
                    if not isinstance(owner, dict) or owner.get("uri") != account or video.get("uri") != uri:
                        unavailable[vid] = "Vimeo did not confirm this recording belongs to the source account."
                    else:
                        video_ids.add(vid)
                        videos.append(video)
            if vid in unavailable:
                recording["issue"] = unavailable[vid]
            else:
                recording["work"] = work_id(institution, "vimeo", vid)


def migrate(institution, root, source, dry_run=False, *, progress=None):
    from filelock import FileLock

    root.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root.parent / ("." + root.name + ".transfer.lock")), timeout=0):
        try:
            return _migrate(institution, root, source, dry_run, refresh=True, progress=progress)
        except RefreshLinks:
            return _migrate(institution, root, source, dry_run, refresh=False, progress=progress)


def _migrate(institution, root, source, dry_run, refresh, progress):
    # The inventory is fully reconciled before creating local records or fetching files.
    videos = list(source.pages("/me/videos"))
    resource_ids = [v["uri"] for v in videos]
    if len(set(resource_ids)) != len(videos) or not all(
        re.fullmatch(r"/videos/[0-9]+", u) for u in resource_ids
    ):
        raise ValueError("Invalid or duplicate video identity")
    if root.exists() and not (root / "migration.json").is_file():
        raise ValueError("Destination belongs to another collection")
    root.mkdir(parents=True, exist_ok=True)
    job = root / "migration.json"
    with source.open(source.base + "/me", True) as response:
        profile = json.load(response)
        account = profile["uri"]
    if not re.fullmatch(r"/users/[0-9]+", account):
        raise ValueError("Expected an authenticated account identifier")
    binding = dict(institution=institution, source=source.base, account=account)
    if job.exists() and json.loads(job.read_text(encoding="utf8")) != binding:
        raise ValueError("Source and destination do not match the previous migration")
    dump(job, binding)
    video_ids = {uri.split("/")[-1] for uri in resource_ids}
    live_event_archives, live_event_archives_issue = live_event_archive_inventory(
        institution, source, account, video_ids,
    )
    resolve_archive_recordings(institution, source, account, live_event_archives, videos, video_ids)
    if progress:
        progress(0, len(videos))
    (root / "objects").mkdir(exist_ok=True)
    showcases, showcase_issue = showcase_inventory(institution, source, account, video_ids, root=root, dry_run=dry_run)
    channels, channel_issue = channel_inventory(institution, source, account, video_ids)
    folders, folder_map, folder_issue = folder_inventory(institution, source, account, profile, videos)
    (root / "checkpoints").mkdir(exist_ok=True)
    works = []
    for video in videos:
        uri = video["uri"]
        vid = uri.split("/")[-1]
        unavailable = {}

        def related(suffix):
            try:
                return list(source.pages(uri + suffix))
            except SourceFailure as exc:
                if exc.status not in (403, 404):
                    raise
                unavailable[suffix] = exc.status
                return []

        tracks = related("/texttracks")
        privacy = video.get("privacy") or {}
        domains = (
            related("/privacy/domains") if privacy.get("embed") == "whitelist" else []
        )
        record = dict(
            id=work_id(institution, "vimeo", vid),
            provider="vimeo",
            source_id=vid,
            source_url=original_url(video, vid),
            title=video["name"],
            description=video.get("description"),
            duration_seconds=video.get("duration"),
            source_privacy=privacy.get("view"),
            source_embed_policy=privacy.get("embed"),
            allowed_domains=[d["domain"] for d in domains],
            publication="private",
            files=[],
        )
        if isinstance(video.get("embed"), dict):
            record["source_embed_appearance"] = embed_appearance(video["embed"])
            ending = embed_ending(video["embed"])
            if ending is not None:
                record["source_embed_ending"] = ending
            logo_url = custom_logo(video["embed"])
            if logo_url is False:
                record["source_embed_logo"] = None
            elif isinstance(logo_url, str) and logo_url:
                if dry_run:
                    record["source_embed_logo_available"] = True
                else:
                    try:
                        record["source_embed_logo"] = store(
                            root,
                            source,
                            logo_url,
                            None,
                            None,
                            max_bytes=4 * 1024**2,
                            check_image=True,
                            vimeo_image=True,
                        )
                    except SourceFailure as exc:
                        if exc.status == 429:
                            raise
                        record["source_embed_logo_issue"] = {"http_status": exc.status}
                    except MediaFailure as exc:
                        record["source_embed_logo_issue"] = {"reason": exc.reason}
        page = video_page(source, video)
        if isinstance(page, dict):
            record["source_watch_appearance"] = watch_appearance(page)
        for family, fields in (
            ("credits", ("uri", "name", "role")),
            ("chapters", ("uri", "title", "timecode")),
        ):
            record[family] = [
                {key: item[key] for key in fields if key in item}
                for item in related("/" + family)
            ]
        versions = [item for item in related("/versions") if isinstance(item, dict)]
        record["versions"] = [
            {key: item[key] for key in ("uri", "version", "active", "created_time") if key in item}
            for item in versions
        ]
        record["unavailable_metadata"] = unavailable
        record["source_privacy_details"] = {
            key: val
            for key, val in privacy.items()
            if isinstance(val, (bool, str))
            and key in ("view", "embed", "download", "add", "comments")
        }
        owner = video.get("user")
        if isinstance(owner, dict) and isinstance(owner.get("name"), str):
            record["source_creator"] = owner["name"]
        record["source_created_time"] = video.get("created_time")
        record["source_modified_time"] = video.get("modified_time")
        for product_field, source_field in (
            ("source_license", "license"),
            ("source_tags", "tags"),
            ("source_categories", "categories"),
            ("source_content_rating", "content_rating"),
        ):
            if source_field in video:
                record[product_field] = video[source_field]
        spatial = video.get("spatial")
        if isinstance(spatial, dict) and isinstance(spatial.get("projection"), str):
            record["source_projection"] = spatial["projection"]
        items = list(assets(video, tracks))
        items.extend(chapter_picture_items(record['chapters'], related, uri))
        checkpoint = root / "checkpoints" / (vid + ".json")
        revision = video.get("modified_time")
        signature = []
        for _, meta, size, source_md5 in items:
            entry = dict(meta, expected_bytes=size)
            if source_md5:
                entry["expected_md5"] = source_md5
            signature.append(entry)
        metadata = dict(record, files=[])
        if not dry_run and revision and checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding="utf8"))
            if (saved["revision"], saved["signature"], saved["metadata"]) == (
                revision,
                signature,
                record,
            ):
                verify_objects = saved["files"]
                if all(
                    re.fullmatch("[0-9a-f]{64}", f["sha256"])
                    and (root / "objects" / f["sha256"]).is_file()
                    and checksum(root / "objects" / f["sha256"]) == f["sha256"]
                    for f in verify_objects
                ):
                    record["files"] = verify_objects
                    if saved.get("caption_status"):
                        record["caption_status"] = saved["caption_status"]
        if not dry_run and not record["files"]:
            for link, meta, size, source_md5 in items:
                role = meta["role"]
                try:
                    if role == "chapter_picture":
                        stored = store(
                            root,
                            source,
                            link,
                            size,
                            source_md5,
                            max_bytes=5*1024**2,
                            check_image=True,
                        )
                    else:
                        stored = store(root, source, link, size, source_md5)
                    record["files"].append(dict(meta, **stored))
                except MediaFailure as exc:
                    if role == "chapter_picture":
                        record.setdefault("chapter_picture_issues", []).append(
                            dict(chapter_uri=meta.get("chapter_uri"), reason=exc.reason)
                        )
                    else:
                        record.setdefault("transfer_issues", []).append(
                            dict(role=role, reason=exc.reason)
                        )
                except SourceFailure as exc:
                    if exc.status == 429:
                        raise
                    if refresh and exc.status in (401, 403, 410):
                        raise RefreshLinks() from None
                    if role == "chapter_picture":
                        record.setdefault("chapter_picture_issues", []).append(
                            dict(chapter_uri=meta.get("chapter_uri"), http_status=exc.status)
                        )
                    else:
                        record.setdefault("transfer_issues", []).append(
                            dict(role=role, http_status=exc.status)
                        )
        roles = (
            {meta["role"] for _, meta, _, _ in items}
            if dry_run
            else {f["role"] for f in record["files"]}
        )
        record["disposition"] = (
            "owner file needed"
            if not roles.intersection({"original", "playback"})
            else "original recovered"
            if "original" in roles
            else "playback copy only"
        )
        if dry_run:
            record["available_assets"] = signature
            record["disposition"] = {
                "original recovered": "original available",
                "playback copy only": "playback available",
            }.get(record["disposition"], record["disposition"])
        if not dry_run:
            from caption_adapter import caption_work

            caption_work(record, root)
            if record.get("transfer_issues") or record.get("chapter_picture_issues"):
                checkpoint.unlink(missing_ok=True)
            else:
                dump(
                    checkpoint,
                    dict(
                        revision=revision,
                        signature=signature,
                        metadata=metadata,
                        files=record["files"],
                        caption_status=record.get("caption_status"),
                    ),
                )
        if uri in folder_map:
            selected = folder_map[uri]
            if selected is None:
                record["source_folder_unavailable"] = True
            else:
                record["source_folder"] = selected["id"]
                record["source_folder_path"] = selected["path"]
        history, recovery = retain_previous_versions(
            root, source, uri, record["title"], versions, dry_run=dry_run, refresh=refresh,
        )
        if history:
            record["versions_history"] = history
        if recovery:
            record["version_recovery"] = recovery
        active = next((item for item in versions if item.get("active") is True), None)
        if active and (source_version := canonical_version_uri(uri, active.get("uri"))):
            record["source_version"] = source_version
            if type(active.get("sequence_number")) is int:
                record["source_version_number"] = active["sequence_number"]
            active_time = active.get("upload_date") or active.get("created_time")
            if isinstance(active_time, str):
                record["version_saved_at"] = active_time
        attach_version_audio(root, source, record, versions, dry_run=dry_run, refresh=refresh)
        attach_previous_text_tracks(root, source, record, dry_run=dry_run, refresh=refresh)
        record["source_review"] = video_review_inventory(source, uri)
        record["source_viewer_comments"] = viewer_comment_inventory(source, uri)
        works.append(record)
        if progress:
            progress(len(works), len(videos))
    manifest = dict(
        schema=1, institution=institution, works=works, showcases=showcases, showcase_issue=showcase_issue,
        channels=channels, channel_issue=channel_issue,
        folders=folders, folder_issue=folder_issue,
        live_event_archives=live_event_archives, live_event_archives_issue=live_event_archives_issue,
    )
    if (creator_profile := vimeo_creator_profile(profile)) is not None:
        manifest["source_profile"] = creator_profile
    dump(root / ("inventory.json" if dry_run else "manifest.json"), manifest)
    if not dry_run:
        verify(root)
    return len(works)
