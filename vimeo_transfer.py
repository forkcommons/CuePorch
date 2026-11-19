"""Read-only Vimeo API transport and private, checkpointed collection migration."""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
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
                            raise ValueError(
                                "Source rate limit reached. Resume later."
                            ) from None
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
    downloads = [
        f
        for f in (video.get("download") or []) + (video.get("files") or [])
        if f.get("link")
        and f.get("quality") != "hls"
        and f.get("type") != "application/x-mpegURL"
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
                source_track=track["uri"],
            ),
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
        )


def store(root, source, link, expected):
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as target:
        temporary = Path(target.name)
    try:
        with temporary.open("wb") as target, source.open(link) as response:
            announced = response.headers.get("Content-Length")
            while block := response.read(1024 * 1024):
                target.write(block)
                digest.update(block)
        size = temporary.stat().st_size
        if not size or any(
            n is not None and int(n) != size for n in (expected, announced)
        ):
            raise ValueError("Incomplete media transfer")
        key = digest.hexdigest()
        temporary.replace(root / "objects" / key)
        return dict(sha256=key, bytes=size)
    finally:
        temporary.unlink(missing_ok=True)


class RefreshLinks(Exception):
    pass


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


def migrate(institution, root, source, dry_run=False):
    from filelock import FileLock

    root.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root.parent / ("." + root.name + ".transfer.lock")), timeout=0):
        try:
            return _migrate(institution, root, source, dry_run, refresh=True)
        except RefreshLinks:
            return _migrate(institution, root, source, dry_run, refresh=False)


def _migrate(institution, root, source, dry_run, refresh):
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
        account = json.load(response)["uri"]
    if not re.fullmatch(r"/users/[0-9]+", account):
        raise ValueError("Expected an authenticated account identifier")
    binding = dict(institution=institution, source=source.base, account=account)
    if job.exists() and json.loads(job.read_text(encoding="utf8")) != binding:
        raise ValueError("Source and destination do not match the previous migration")
    dump(job, binding)
    showcases, showcase_issue = showcase_inventory(institution, source, account,
                                                   {uri.split("/")[-1] for uri in resource_ids})
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
            ("versions", ("uri", "version", "active", "created_time")),
        ):
            record[family] = [
                {key: item[key] for key in fields if key in item}
                for item in related("/" + family)
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
        signature = [dict(meta, expected_bytes=size) for _, meta, size in items]
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
            for link, meta, size in items:
                try:
                    record["files"].append(
                        dict(meta, **store(root, source, link, size))
                    )
                except SourceFailure as exc:
                    if refresh and exc.status in (401, 403, 410):
                        raise RefreshLinks() from None
                    record.setdefault("transfer_issues", []).append(
                        dict(role=meta["role"], http_status=exc.status)
                    )
        roles = (
            {meta["role"] for _, meta, _ in items}
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
        works.append(record)
    manifest = dict(schema=1, institution=institution, works=works, showcases=showcases, showcase_issue=showcase_issue)
    dump(root / ("inventory.json" if dry_run else "manifest.json"), manifest)
    if not dry_run:
        verify(root)
    return len(works)
