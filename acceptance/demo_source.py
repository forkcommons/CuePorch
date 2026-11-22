"""An account-free source rehearsal using portable media and Vimeo's documented HTTP shapes."""

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def version_audio_fixture(directory):
    files = {}
    for name, frequency, codec in (
            ("version-203-es.wav", 880, "pcm_s16le"),
            ("version-202-en.mp3", 660, "libmp3lame")):
        target = directory / name
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency={frequency}:duration=2",
            "-c:a", codec, str(target),
        ], check=True)
        files[name] = target
    return files


def build_server(directory, port=0, *, version_fixture=False, audio_fixture=False):
    directory.mkdir(parents=True, exist_ok=True)
    clip = directory / "meeting.mp4"
    if not clip.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24:d=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=620:duration=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(clip),
            ],
            check=True,
        )
    media = clip.read_bytes()
    previous = {}
    if version_fixture:
        for extension, dimensions, video_codec, audio_codec in (
            ("mp4", "256x144", "libx264", "aac"),
            ("webm", "192x108", "libvpx-vp9", "libopus"),
        ):
            target = directory / f"previous.{extension}"
            if not target.exists():
                subprocess.run([
                    "ffmpeg", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=s={dimensions}:r=24:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
                    "-c:v", video_codec, "-pix_fmt", "yuv420p", "-c:a", audio_codec, str(target),
                ], check=True)
            previous[extension] = target.read_bytes()
    audio = version_audio_fixture(directory) if audio_fixture else {}
    caption = (
        b"WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nThe public meeting is in session.\n"
    )

    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            content_type = "application/json"
            if path == "/fixture/legacy-player":
                content_type = "text/html; charset=utf-8"
                data = b'<!doctype html><title>Legacy test player</title><video controls src="/media/72.mp4"></video>'
            elif version_fixture and path in ("/media/previous.mp4", "/media/previous.webm"):
                if self.headers.get("Authorization"):
                    return self.send_error(400)
                extension = path.rsplit(".", 1)[1]
                content_type = "video/" + extension
                data = previous[extension]
            elif audio_fixture and path.rsplit("/", 1)[0] == "/media" and path.rsplit("/", 1)[1] in audio:
                if self.headers.get("Authorization"):
                    return self.send_error(400)
                name = path.rsplit("/", 1)[1]
                data = audio[name].read_bytes()
                content_type = "audio/wav" if name.endswith(".wav") else "audio/mpeg"
            elif path == "/media/previous-fr.vtt":
                if self.headers.get("Authorization"):
                    return self.send_error(400)
                content_type = "text/vtt; charset=utf-8"
                data = b"WEBVTT\n\n00:00:00.100 --> 00:00:02.500\nEarlier cut caption\n"
            elif path == "/media/previous-en.srt":
                if self.headers.get("Authorization"):
                    return self.send_error(400)
                content_type = "application/x-subrip"
                data = b"1\n00:00:00,100 --> 00:00:02,500\nEarlier cut English\n\n"
            elif path in ("/media/chapter.png", "/media/vimeo-logo.png"):
                if self.headers.get("Authorization"):
                    return self.send_error(400)
                import base64
                content_type = "image/png"
                data = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPw4AAAAASUVORK5CYII="
                )
            elif path.startswith("/media/"):
                content_type = "text/vtt" if path.endswith(".vtt") else "video/mp4"
                data = caption if path.endswith(".vtt") else media
            else:
                if self.headers.get("Authorization") != "Bearer cueporch-fixture":
                    return self.send_error(401)
                origin = f"http://127.0.0.1:{self.server.server_port}"
                if path == "/me":
                    body = {"uri": "/users/10"}
                    if getattr(self.server, "folder_fixture", False):
                        body["metadata"] = {"connections": {"folders": {"uri": "/users/10/projects"}}}
                    if getattr(self.server, "profile_fixture", False):
                        body.update(name="Gallery Archive", bio="Films and exhibitions. <em>Archive</em>",
                                    email="owner-only@example.org", profile_discovery=True, websites=[
                                        {"link": "https://gallery.example.org/films"},
                                        {"link": "https://gallery.example.org/films"},
                                        {"link": "javascript:alert(1)"},
                                        {"link": "https://user:secret@example.net/account"},
                                        {"link": "https://example.org:99999/invalid-port"},
                                    ])
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == '/videos/71' and parse_qs(urlsplit(self.path).query).get('fields') == ['uri,page']:
                    page = getattr(self.server, 'video_page_fixture', None)
                    if page is None:
                        return self.send_error(403)
                    body = json.dumps(dict(uri=path, page=page)).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/videos/998" and getattr(self.server, "live_event_fixture", False) and not urlsplit(self.path).query:
                    body = dict(
                        uri=path,
                        name="Archive-only meeting",
                        user=dict(uri="/users/10", name="Station team"),
                        privacy=dict(view="nobody", embed="private"),
                        duration=4,
                        modified_time="2026-11-03",
                        download=[dict(quality="source", type="video/mp4", size=len(media), link=origin + "/media/998.mp4")],
                    )
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if version_fixture and path in ('/videos/71', '/videos/72'):
                    entries = []
                    if path == '/videos/71':
                        first = dict(
                            uri='/videos/71/review_links/7f4f9c2e-3b1a-4c8d-9e2f-6a5b4c3d2e1f',
                            name='Panel feedback',
                            is_enabled=True,
                            expires_on='2036-12-24T19:30:00Z',
                            password='fixture-sensitive-review-password',
                            url='https://vimeo.com/fixture-sensitive-vimeo-access',
                            is_dark=True,
                            allow_comments=True,
                            allow_downloads=False,
                            allow_status_change=True,
                            hide_end_cards=True,
                            show_all_versions=True,
                            show_resolved_comments=True,
                            show_my_logo=True,
                        )
                        entries = [
                            first,
                            first | dict(
                                uri='/videos/71/review_links/1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d',
                                name='Expired panel feedback',
                                is_enabled=False,
                                expires_on='2020-12-31T23:59:59Z',
                            ),
                            first | dict(
                                uri='/videos/999/review_links/2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e',
                                name='Wrong video',
                                is_enabled=False,
                                expires_on='2020-12-31T23:59:59Z',
                            ),
                        ]
                    body = json.dumps(dict(review_status=getattr(self.server, 'review_status', 'needs_review'), review_links=entries)).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if version_fixture and path in ("/videos/71/versions/200", "/videos/71/versions/202"):
                    extension, number, height = ("mp4", 2, 144) if path.endswith("/202") else ("webm", 1, 108)
                    data = json.dumps(dict(
                        uri=path, active=False, sequence_number=number, duration=3,
                        created_time=f"2026-09-0{number}T12:00:00Z",
                        play=dict(status="playable", progressive=[dict(
                            link=f"{origin}/media/previous.{extension}?fixture-signature=short-lived",
                            type="video/" + extension, height=height, size=len(previous[extension]),
                        )]),
                    )).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if version_fixture and path == "/videos/71/versions/201":
                    return self.send_error(403)
                if audio_fixture and path.endswith("/audiotracks"):
                    if path == "/videos/71/versions/201/audiotracks":
                        return self.send_error(403)
                    specs = {
                        "/videos/71/versions/203/audiotracks": [
                            ("main", "es", "version-203-es.wav"), ("descriptive", "en", None),
                        ],
                        "/videos/71/versions/202/audiotracks": [("commentary", "en", "version-202-en.mp3")],
                    }
                    if path not in specs and path != "/videos/71/versions/200/audiotracks":
                        return self.send_error(404)
                    data = json.dumps([
                        dict(uri=f"{path}/fixture-{kind}-{lang}", type=kind, language=dict(code=lang),
                             active=True, transcode=dict(status="complete"), download=[
                                 dict(rendition="source", extension=name.rsplit(".", 1)[1],
                                      link=f"{origin}/media/{name}?fixture-signature=short-lived")
                             ] if name else [])
                        for kind, lang, name in specs.get(path, [])
                    ]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                following = None
                total = None
                if path == "/users/10/albums":
                    values = [dict(uri="/users/10/albums/805", name="Station and Gallery",
                                   description="Local programming and an artist screening", privacy="password")]
                elif path == "/users/10/albums/805/videos":
                    page = parse_qs(urlsplit(self.path).query).get("page", ["1"])[0]
                    subset = ["72", "71"] if page == "1" else ["999"]
                    following = path + "?per_page=100&page=2" if page == "1" else None
                    total = 3
                    values = [{"uri": "/videos/" + uid} for uid in subset]
                elif path == "/users/10/live_events" and getattr(self.server, "live_event_fixture", False):
                    if getattr(self.server, "live_event_denied", False):
                        self.send_error(403)
                        return
                    page = parse_qs(urlsplit(self.path).query).get("page", ["1"])[0]
                    following = path + "?per_page=100&page=2" if page == "1" else None
                    total = 2
                    if page == "1":
                        values = [{"uri": "/live_events/801", "title": "Council replay archive",
                                   "stream_privacy": {"view": "team"},
                                   "stream_description": "Recurring council meeting",
                                   "preferred_stream_method": "encoder", "latency": "low",
                                   "chat_enabled": True, "dvr": False, "auto_cc_enabled": True,
                                   "auto_cc_language": "fr", "has_forced_lead_capture_form": True,
                                   "schedule": dict(start_time="2030-01-07T10:00:00-07:00",
                                                    end_time="2030-01-07T11:00:00-07:00",
                                                    time_zone="America/Denver", rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=4"),
                                   "recordings": [{"uri": "/videos/71", "title": "Council replay",
                                                   "streamed_on": "2026-11-03T18:30:45.125-07:00"},
                                                  {"uri": "/videos/999", "title": "Missing replay",
                                                   "streamed_on": "2026-11-03T18:30:45"},
                                                  {"uri": "/videos/998", "title": "Archive-only meeting",
                                                   "streamed_on": None}],
                                   "streamable_clip": "/videos/72", "stream_key": "fixture-ingest-key"}]
                    else:
                        values = [{"uri": "/live_events/802", "title": "No past streams", "recordings": [],
                                   "schedule": dict(start_time="2030-01-07T10:00:00-07:00",
                                                    end_time="2030-01-07T11:00:00-07:00",
                                                    time_zone="America/Denver", rrule="FREQ=YEARLY")}]
                elif path == "/users/10/projects" and getattr(self.server, "folder_fixture", False):
                    page = parse_qs(urlsplit(self.path).query).get("page", ["1"])[0]
                    root_uri, root_title = "/users/10/projects/100", "Foundation archive"
                    meta = {"connections": {"ancestor_path": [
                        {"uri": root_uri, "name": root_title}]}}
                    if page == "1":
                        values = [
                            {"uri": root_uri, "name": root_title},
                            {"uri": "/users/10/projects/102", "name": "Empty vault", "metadata": meta, "settings": {"color": "#445566"}},
                        ]
                        following = path + "?per_page=100&page=2"
                    else:
                        values = [
                            {"uri": "/users/10/projects/101", "name": "Gallery edits", "metadata": meta, "settings": {"color": "#445566"}},
                        ]
                        following = None
                    total = 3
                elif path == "/me/videos":
                    values = [
                        dict(
                            uri="/videos/71",
                            name="Station meeting",
                            privacy=dict(view="anybody" if (getattr(self.server, "public_viewer_fixture", False) or not audio_fixture) else "password", embed="public"),
                            duration=4,
                            user=dict(name="Station team"),
                            modified_time="2026-11-03",
                            **(
                                {"embed": self.server.video_embed_fixture}
                                if isinstance(getattr(self.server, "video_embed_fixture", None), dict)
                                else {}
                            ),
                            download=[
                                dict(
                                    quality="source",
                                    type="video/mp4",
                                    size=len(media),
                                    link=origin + "/media/71.mp4",
                                )
                            ],
                        ),
                        dict(
                            uri="/videos/72",
                            name="Exhibition cut",
                            privacy=dict(view="nobody", embed="whitelist"),
                            duration=4,
                            user=dict(name="Independent artist"),
                            modified_time=getattr(self.server, "second_video_revision", "2026-11-01"),
                            download=[
                                dict(
                                    quality="hd",
                                    type="video/mp4",
                                    size=len(media),
                                    link=origin + "/media/72.mp4",
                                )
                            ],
                        ),
                        dict(
                            uri="/videos/73",
                            name="Exhibition cut",
                            privacy=dict(view="nobody", embed="private"),
                            duration=4,
                            user=dict(name="Independent artist"),
                            modified_time="2026-11-02",
                            download=[],
                        ),
                    ]
                    if getattr(self.server, "folder_fixture", False):
                        folders = {
                            "/videos/71": {"uri": "/users/10/projects/101", "name": "Gallery edits"},
                            "/videos/72": {"uri": "/users/10/projects/100", "name": "Foundation archive"},
                        }
                        for v in values:
                            if v.get("uri") in folders:
                                v["parent_folder"] = folders[v["uri"]]
                    if getattr(self.server, "privacy_fixture", None):
                        stage = self.server.privacy_fixture
                        for identifier, title, privacy, embed, download in (
                            (74, "Migrated unlisted", "unlisted" if stage == "initial" else "nobody", "private", True),
                            (75, "Migrated password", "password", "whitelist", False),
                            (76, "Migrated embed", "disable", "whitelist", False),
                            (77, "Missing embed allowlist", "disable", "whitelist", False),
                        ):
                            values.append(dict(
                                uri=f"/videos/{identifier}", name=title, duration=4,
                                link=f"https://vimeo.com/{identifier}/abcdef1234" if identifier == 74 and stage == "initial" else f"https://vimeo.com/{identifier}",
                                modified_time="2026-11-10" if stage == "initial" or identifier != 74 else "2026-11-18",
                                privacy=dict(view=privacy, embed=embed, download=download),
                                download=[dict(quality="hd", type="video/mp4", size=len(media),
                                               link=f"{origin}/media/{identifier}.mp4")],
                            ))
                            if identifier == 74:
                                values[-1].update(license="by", tags=[{"tag": "city council"}],
                                                  categories=[{"name": "Documentary"}],
                                                  content_rating=["advertisement", "language"],
                                                  spatial={"projection": "equirectangular"})
                    extra_inventory = getattr(self.server, "extra_inventory_count", 0)
                    if extra_inventory:
                        values.extend(dict(uri=f"/videos/{identifier}", name=f"Archive record {identifier}",
                                           privacy=dict(view="nobody", embed="private"), download=[])
                                      for identifier in range(1000, 1000 + extra_inventory))
                        total = len(values)
                        page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
                        start = (page - 1) * 100
                        values = values[start:start + 100]
                        if start + 100 < total:
                            following = f"{path}?per_page=100&page={page + 1}"
                elif getattr(self.server, 'viewer_comments_fixture', False) and path in (
                    '/videos/71/comments',
                    '/videos/71/comments/501/replies',
                    '/videos/72/comments',
                ):
                    if path == '/videos/71/comments':
                        values = [
                            {
                                'id': 501,
                                'uri': '/videos/71/comments/501',
                                'type': 'video',
                                'created_on': '2020-01-02T03:04:05Z',
                                'text': getattr(self.server, 'viewer_comment_text', 'Historic festival discussion'),
                                'replies': {'total': 1},
                                'metadata': {
                                    'connections': {
                                        'user': {'name': 'Festival Viewer'},
                                    },
                                },
                            },
                            {
                                'id': 502,
                                'uri': '/videos/71/comments/502',
                                'type': 'video',
                                'created_on': '2020-01-02T03:04:05Z',
                                'text': 'Matinee screening',
                                'replies': {'total': 0},
                                'metadata': {
                                    'connections': {
                                        'user': {'name': 'Festival Viewer'},
                                    },
                                },
                            },
                            {
                                'id': 991,
                                'uri': '/videos/999/comments/991',
                                'type': 'video',
                                'created_on': '2020-01-02T03:04:05Z',
                                'text': 'Foreign archive note',
                                'replies': {'total': 0},
                                'metadata': {
                                    'connections': {
                                        'user': {'name': 'Festival Viewer'},
                                    },
                                },
                            },
                            {
                                'id': 503,
                                'uri': '/videos/71/comments/503',
                                'type': 'video',
                                'created_on': '2020-01-02T03:04:05Z',
                                'text': 'Removed festival note',
                                'deleted_on': '2020-02-03T04:05:06Z',
                                'replies': {'total': 0},
                                'metadata': {
                                    'connections': {
                                        'user': {'name': 'Festival Viewer'},
                                    },
                                },
                            },
                        ]
                    elif path == '/videos/71/comments/501/replies':
                        fields = parse_qs(urlsplit(self.path).query).get('fields', [''])[0].split(',')
                        if {'user.name', 'metadata.connections.replies.total'}.intersection(fields):
                            return self.send_error(400)
                        values = [
                            {
                                'id': 601,
                                'uri': '/videos/71/comments/501/replies/601',
                                'type': 'video',
                                'created_on': '2020-01-02T03:04:05Z',
                                'text': 'Historic festival reply',
                                'replies': {'total': 0},
                                'metadata': {
                                    'connections': {
                                        'guest_user': {'name': 'Guest Reviewer'},
                                    },
                                },
                            },
                        ]
                    elif path == '/videos/72/comments':
                        values = [
                            {
                                'id': 701,
                                'uri': '/videos/72/comments/701',
                                'type': 'video',
                                'created_on': '2020-01-02T03:04:05Z',
                                'text': 'Restricted archive message',
                                'replies': {'total': 0},
                                'metadata': {
                                    'connections': {
                                        'user': {'name': 'Festival Viewer'},
                                    },
                                },
                            },
                        ]
                    if path == '/videos/71/comments' and getattr(self.server,'new_viewer_comment', False):
                        values.append({
                            'uri': '/videos/71/comments/504',
                            'type': 'video',
                            'text': 'Approval test viewer note',
                            'created_on': '2020-01-03T03:04:05Z',
                            'replies': {'total':0},
                            'metadata': {'connections': {'user': {'name':'Second Festival Viewer'}}},
                        })
                    for row in values:
                        row['metadata']['connections']['replies'] = row.pop('replies')
                    if path == '/videos/71/comments':
                        page = int(parse_qs(urlsplit(self.path).query).get('page', ['1'])[0])
                        total = len(values)
                        if page == 1:
                            values = values[:1]
                            following = path + '?page=2'
                        elif page == 2:
                            values = values[1:]
                elif path == "/videos/72/privacy/domains":
                    values = [{"domain": "museum.example"}]
                elif path in ("/videos/75/privacy/domains", "/videos/76/privacy/domains") and getattr(self.server, "privacy_fixture", None):
                    values = [{"domain": self.server.privacy_domain}]
                elif version_fixture and path == "/videos/71/versions/202/texttracks":
                    values = [
                        {
                            "uri": "/videos/71/versions/202/texttracks/401",
                            "language": "fr",
                            "type": "subtitles",
                            "display_language": "French",
                            "active": True,
                            "download_links": {
                                "vtt": origin + "/media/previous-fr.vtt?fixture-signature=timed"
                            },
                        },
                        {
                            "uri": "/videos/71/versions/202/texttracks/402",
                            "language": "es",
                            "type": "captions",
                            "display_language": "Spanish",
                            "active": True,
                        },
                        {
                            "uri": "/videos/71/versions/202/texttracks/403",
                            "language": "de",
                            "type": "subtitles",
                            "display_language": "German",
                            "active": False,
                            "download_links": {
                                "vtt": origin + "/media/disabled-de.vtt"
                            },
                        },
                        {
                            "uri": "/videos/71/versions/200/texttracks/401",
                            "language": "fr",
                            "type": "subtitles",
                            "active": False,
                        },
                        {
                            "uri": "/videos/71/texttracks/404",
                            "language": "en",
                            "type": "captions",
                            "active": True,
                            "download_links": {"srt": origin + "/media/previous-en.srt"},
                        },
                    ]
                elif version_fixture and path.startswith("/videos/71/versions/") and path.endswith("/texttracks"):
                    values = []
                elif path.endswith("/texttracks"):
                    values = (
                        [
                            dict(
                                uri=path + "/1",
                                language="en",
                                type="subtitles",
                                link=origin + "/media/captions.vtt",
                            )
                        ]
                        if "/71/" in path
                        else []
                    )
                elif path.endswith("/chapters"):
                    if getattr(self.server, "chapter_picture_fixture", False) and path == "/videos/71/chapters":
                        values = [
                            dict(uri=path + "/18", title="Opening", timecode=0),
                            dict(uri=path + "/19", title="Next chapter", timecode=2),
                        ]
                    else:
                        values = (
                            [dict(title="Opening", timecode=0)] if "/71/" in path else []
                        )
                elif path == "/videos/71/chapters/18/pictures":
                    values = [
                        dict(
                            uri=path + "/fixture18",
                            active=True,
                            sizes=[dict(width=320, link=origin + "/media/chapter.png")],
                        ),
                        dict(
                            uri=path + "/fixture19",
                            active=False,
                            sizes=[dict(width=640, link=origin + "/media/missing-chapter.png")],
                        ),
                    ]
                elif path.endswith("/credits"):
                    values = [dict(name="Fixture station", role="Producer")]
                elif path.endswith("/versions"):
                    values = [
                        dict(uri=f"/videos/71/versions/{uid}", sequence_number=number, active=uid == 203)
                        for uid, number in ((203, 4), (202, 2), (201, 3), (200, 1))
                    ] if version_fixture and path == "/videos/71/versions" else []
                else:
                    return self.send_error(404)
                data = json.dumps(
                    dict(data=values, total=len(values) if total is None else total, paging=dict(next=following))
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), API)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--port", type=int, default=19002)
    a = p.parse_args()
    with build_server(a.directory, a.port) as server:
        print(
            "Simulated source on http://127.0.0.1:" + str(server.server_port),
            flush=True,
        )
        server.serve_forever()
