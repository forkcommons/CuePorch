"""Read-only Vimeo API transport and private, checkpointed collection migration."""

from collections import defaultdict, deque
import hashlib
from http.client import HTTPException
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cueporch import checksum, dump, verify, work_id


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

    def open(self, url, authenticated=False):
        for redirects in range(7):
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
        raise ValueError("Media redirect limit exceeded")

    def pages(self, path):
        url = self.base + path + "?per_page=100"
        visited = set()
        count = 0
        total = None
        while url:
            if url in visited or urlsplit(url).path != path:
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


def store(root, source, link, expected, source_md5):
    digest = hashlib.sha256()
    if source_md5 and (not isinstance(source_md5, str) or not re.fullmatch("[a-fA-F0-9]{32}", source_md5)):
        raise MediaFailure("invalid_source_checksum")
    source_digest = hashlib.md5(usedforsecurity=False) if source_md5 else None
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as target:
        temporary = Path(target.name)
    try:
        try:
            response = source.open(link)
        except INTERRUPTED:
            raise MediaFailure("media_connection_interrupted") from None
        with response, temporary.open("wb") as target:
            announced = response.headers.get("Content-Length")
            while True:
                try:
                    block = response.read(1024 * 1024)
                except INTERRUPTED:
                    raise MediaFailure("media_connection_interrupted") from None
                if not block:
                    break
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


def showcase_inventory(institution, source, account, video_ids):
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
        if visibility not in ("anybody", "public", "nobody", "private", "password", "embed_only"):
            visibility = "unknown"
        title, description = item.get("name"), item.get("description")
        result.append(dict(id=work_id(institution, "vimeo-showcase", canonical),
                           uri=canonical, source_id=identifier, source_url=f"https://vimeo.com/showcase/{identifier}",
                           title=(title.strip()[:128] if isinstance(title, str) else "") or "Untitled showcase",
                           description=description.strip()[:5000] if isinstance(description, str) else "",
                           source_visibility=visibility, members=list(dict.fromkeys(recovered)),
                           missing=list(dict.fromkeys(missing)), issues=issues))
    return result, None


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
    if progress:
        progress(0, len(videos))
    showcases, showcase_issue = showcase_inventory(institution, source, account,
                                                   {uri.split("/")[-1] for uri in resource_ids})
    folders, folder_map, folder_issue = folder_inventory(institution, source, account, profile, videos)
    (root / "objects").mkdir(exist_ok=True)
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
                try:
                    record["files"].append(
                        dict(meta, **store(root, source, link, size, source_md5))
                    )
                except MediaFailure as exc:
                    record.setdefault("transfer_issues", []).append(
                        dict(role=meta["role"], reason=exc.reason)
                    )
                except SourceFailure as exc:
                    if exc.status == 429:
                        raise
                    if refresh and exc.status in (401, 403, 410):
                        raise RefreshLinks() from None
                    record.setdefault("transfer_issues", []).append(
                        dict(role=meta["role"], http_status=exc.status)
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
            if record.get("transfer_issues"):
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
        works.append(record)
        if progress:
            progress(len(works), len(videos))
    manifest = dict(
        schema=1, institution=institution, works=works, showcases=showcases, showcase_issue=showcase_issue,
        folders=folders, folder_issue=folder_issue,
    )
    dump(root / ("inventory.json" if dry_run else "manifest.json"), manifest)
    if not dry_run:
        verify(root)
    return len(works)
