"""Publish a portable CuePorch work through PeerTube's resumable upload API."""

import hashlib
import argparse
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from catalog import cut_chapters
from cueporch import dump, verify
from library_languages import tracks_for


class SourceReplacementUnavailable(ValueError):
    pass


class PeerTube:
    def __init__(self, origin, username, password, *, access=None):
        self.origin = origin.rstrip("/")
        parts = urlsplit(self.origin)
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("PeerTube requires HTTPS or a local test host")
        self.session = requests.Session()
        if access:
            authorization, self.expires_at, self.user = access
            self.session.headers["Authorization"] = authorization
            return
        client = self.call("GET", "/api/v1/oauth-clients/local").json()
        token = self.call(
            "POST",
            "/api/v1/users/token",
            data=dict(
                username=username,
                password=password,
                grant_type="password",
                client_id=client["client_id"],
                client_secret=client["client_secret"],
            ),
        ).json()
        self.expires_at = time.monotonic() + max(0, float(token["expires_in"]) - 30)
        self.session.headers["Authorization"] = "Bearer " + token["access_token"]
        self.user = self.call("GET", "/api/v1/users/me").json()

    def call(self, method, path, **options):
        url = urljoin(self.origin, path)
        if urlsplit(url)[:2] != urlsplit(self.origin)[:2]:
            raise ValueError("Unexpected PeerTube origin")
        response = self.session.request(
            method, url, allow_redirects=False, timeout=(15, 180), **options
        )
        if response.status_code == 401:
            self.expires_at = 0
        if response.status_code != 308:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("Unexpected API redirect")
        return response

    def set_public(self, video_id, public):
        self.call(
            "PUT", "/api/v1/videos/" + video_id, json={"privacy": 1 if public else 3}
        )

    def update_metadata(self, video_id, metadata, thumbnail=None):
        path = "/api/v1/videos/" + video_id
        if thumbnail is None:
            self.call("PUT", path, json=metadata)
            return
        with thumbnail.open("rb") as stream:
            header = stream.read(16)
            stream.seek(0)
            kind = (
                "png"
                if header.startswith(b"\x89PNG")
                else "webp"
                if header[8:12] == b"WEBP"
                else "jpeg"
            )
            self.call(
                "PUT",
                path,
                data=metadata,
                files={"thumbnailfile": ("poster." + kind, stream, "image/" + kind)},
            )

    def upload(self, root, work, media, state, receipt):
        size = media["bytes"]
        replacing = state.get("video") is not None
        marker = (
            "CuePorch intake "
            + hashlib.sha256((work["id"] + media["sha256"]).encode()).hexdigest()[:32]
        )
        if not replacing and "upload" in state:
            offset = 0
            while True:
                page = self.call(
                    "GET",
                    "/api/v1/users/me/videos",
                    params={"start": offset, "count": 100},
                ).json()
                matches = [
                    v
                    for v in page["data"]
                    if v["name"] == marker and v["privacy"]["id"] == 3
                ]
                if matches:
                    if len(matches) != 1:
                        raise ValueError("Ambiguous destination work identity")
                    state["video"] = matches[0]["uuid"]
                    state.pop("upload")
                    dump(receipt, state)
                    return
                offset += len(page["data"])
                if offset >= page["total"]:
                    break
                if not page["data"]:
                    raise ValueError("Incomplete destination inventory")
        if "upload" not in state:
            name = work["id"] + "." + media["format"]
            endpoint = (
                "/api/v1/videos/" + state["video"] + "/source/replace-resumable"
                if replacing
                else "/api/v1/videos/upload-resumable"
            )
            metadata = (
                {"filename": name}
                if replacing
                else dict(
                    filename=name,
                    name=marker,
                    channelId=self.user["videoChannels"][0]["id"],
                    privacy=3,
                    **(
                        {"description": work["description"][:10000]}
                        if work.get("description")
                        else {}
                    ),
                    waitTranscoding=True,
                    downloadEnabled=False,
                )
            )
            try:
                response = self.call(
                    "POST",
                    endpoint,
                    json=metadata,
                    headers={
                        "X-Upload-Content-Length": str(size),
                        "X-Upload-Content-Type": mimetypes.guess_type(name)[0]
                        or "application/octet-stream",
                    },
                )
            except requests.HTTPError as error:
                if replacing and error.response.status_code == 403:
                    raise SourceReplacementUnavailable(
                        "PeerTube declined replacement. Check video_file.update.enabled "
                        "and confirm the connected account can edit the existing video."
                    ) from error
                raise
            state["upload"] = response.headers["Location"]
            dump(receipt, state)
        path = state["upload"]
        with (root / "objects" / media["sha256"]).open("rb") as stream:
            try:
                response = self.call(
                    "PUT", path, headers={"Content-Range": "bytes */" + str(size)}
                )
            except requests.HTTPError as exc:
                if exc.response.status_code == 404:
                    state.pop("upload", None)
                    dump(receipt, state)
                    raise ValueError(
                        "Expired upload session reconciled; retry starts a fresh session"
                    ) from None
                raise
            while response.status_code == 308:
                received = response.headers.get("Range")
                offset = 0
                if received and received != "bytes=0--1":
                    match = re.fullmatch(r"bytes=0-(\d+)", received)
                    if not match:
                        raise ValueError(
                            f"Unexpected resumable upload offset {received!r}"
                        )
                    offset = int(match[1]) + 1
                stream.seek(offset)
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    raise ValueError("Remote upload has no completion receipt")
                response = self.call(
                    "PUT",
                    path,
                    data=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                    },
                )
            if not replacing:
                state["video"] = response.json()["video"]["uuid"]
            state["media"] = media["sha256"]
            state.pop("upload", None)
            state.pop("incoming_media", None)
            dump(receipt, state)

    def publish(self, root, work, receipt, public=False, thumbnail=None):
        media = next(f for f in work["files"] if f["role"] in ("original", "playback"))
        identity = dict(
            work=work["id"],
            provider=self.origin,
            account=self.user["id"],
            media=media["sha256"],
        )
        state = json.loads(receipt.read_text()) if receipt.exists() else dict(identity)
        if any(state.get(k) != identity[k] for k in ("work", "provider", "account")):
            raise ValueError(
                "Destination receipt does not match work, source media or account"
            )
        previous = next(
            (version for version in reversed(work.get("versions_history", []))
             if any(f["sha256"] == state["media"] and f["role"] in ("original", "playback")
                    for f in version["files"])),
            {},
        )
        if "library_version_changed" in work and "caption_languages" not in state:
            legacy_languages = [
                f.get("language") or "und" for f in previous.get("files", [])
                if f["role"] == "caption"
            ]
            supported = self.call("GET", "/api/v1/videos/languages").json() if legacy_languages else {}
            state["caption_languages"] = sorted({
                language if language in supported else language.split("-")[0]
                for language in legacy_languages
            } & set(supported))
            state["has_chapters"] = bool(previous.get("chapters"))
            dump(receipt, state)
        if "video" in state and not public and state.get("visibility") == "public":
            self.call("PUT", "/api/v1/videos/" + state["video"], json={"privacy": 3})
            state["visibility"] = "private"
            dump(receipt, state)
        if (
            state.get("video")
            and state["media"] != media["sha256"]
            and state.get("incoming_media") != media["sha256"]
        ):
            state.pop("upload", None)
            state["incoming_media"] = media["sha256"]
            dump(receipt, state)
        if "video" not in state or state["media"] != media["sha256"]:
            self.upload(root, work, media, state, receipt)
        path = "/api/v1/videos/" + state["video"]
        metadata = {"name": work["title"][:120]}
        description = work.get("description") or ""
        credits = work.get("credits") or ""
        if isinstance(credits, list):
            credits = ", ".join(
                " ".join(str(c.get(k) or "") for k in ("name", "role")).strip()
                for c in credits
            )
        if credits:
            description = (description + "\n\nCredits: " + credits).strip()
        if description:
            metadata["description"] = description[:10000]
        if thumbnail is None:
            poster = next((f for f in work["files"] if f["role"] == "poster"), None)
            thumbnail = poster["sha256"] if poster else None
        self.update_metadata(
            state["video"], metadata, root / "objects" / thumbnail if thumbnail else None
        )
        captions = tracks_for(work, vtt_only=False)
        supported = (
            self.call("GET", "/api/v1/videos/languages").json() if captions else {}
        )
        attached = set()
        state["warnings"] = []
        for caption in captions:
            source_language = caption.get("language") or "und"
            language = (
                source_language
                if source_language in supported
                else source_language.split("-")[0]
            )
            if language not in supported or language in attached:
                state["warnings"].append(
                    f"Caption {source_language} retained locally; destination needs a supported, distinct language."
                )
                continue
            attached.add(language)
            with (root / "objects" / caption["sha256"]).open("rb") as stream:
                self.call(
                    "PUT",
                    path + "/captions/" + language,
                    files={"captionfile": ("captions.vtt", stream, "text/vtt")},
                )
        for language in set(state.get("caption_languages", [])) - attached:
            try:
                self.call("DELETE", path + "/captions/" + language)
            except requests.HTTPError as error:
                if error.response.status_code != 404:
                    raise
        state["caption_languages"] = sorted(attached)
        selected_chapters = cut_chapters(work)
        if selected_chapters or state.get("has_chapters"):
            chapters = [
                {"title": chapter["title"], "timecode": int(chapter["timecode"])}
                for chapter in selected_chapters
            ]
            self.call("PUT", path + "/chapters", json={"chapters": chapters})
        state["has_chapters"] = bool(selected_chapters)
        deadline = time.monotonic() + int(
            os.environ.get("CUEPORCH_PROCESSING_TIMEOUT", "3600")
        )
        while time.monotonic() < deadline:
            video = self.call("GET", path).json()
            if video["state"]["id"] == 1:
                break
            time.sleep(2)
        else:
            raise ValueError("PeerTube processing is pending; resume the saved receipt")
        if public and work.get("source_privacy") not in ("anybody", "public"):
            raise ValueError(
                "Source viewing permission does not allow public publication"
            )
        if public and work.get("source_embed_policy") not in ("public", "anywhere"):
            raise ValueError("Source embed permission needs reconciliation")
        self.call(
            "PUT",
            path,
            json={"privacy": 1 if public else 3, "name": work["title"][:120]},
        )
        state["visibility"] = "public" if public else "private"
        dump(receipt, state)
        return state["video"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("collection", type=Path)
    parser.add_argument("work_id")
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    verify(args.collection)
    manifest = json.loads(
        (args.collection / "manifest.json").read_text(encoding="utf8")
    )
    work = next(w for w in manifest["works"] if w["id"] == args.work_id)
    peer = PeerTube(
        args.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
    )
    print(peer.publish(args.collection, work, args.receipt, args.public))


if __name__ == "__main__":
    main()
