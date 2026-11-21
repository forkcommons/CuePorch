"""Hold complete DASH fragments for a delayed, revocable event playback window."""

from dataclasses import replace
from pathlib import Path
from threading import Lock
from time import monotonic, time

from event_dash_format import INIT_FILE, MAX_SEGMENTS, MEDIA_FILE, master_playlist, media_playlist, parse_manifest


TARGET_DELAY = 60
PLAYLIST_GRACE = 20
MAX_FRAGMENT_BYTES = 32 * 1024 * 1024
MAX_SPOOL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FUTURE_FRAGMENTS = 40


class ConflictingFragment(ValueError):
    """A contribution attempted to replace a committed media object."""


class FullBuffer(ValueError):
    """The occurrence reached its storage allowance."""


class Buffer:
    def __init__(self, directory: Path, identity, window, clock=monotonic):
        self.path = directory
        self.identity = identity
        self.window = window
        self.clock = clock
        self.lock = Lock()
        self.tracks = {}
        self.objects = {}
        self.bytes = 0
        self.origin = None
        self.starts_at = None
        self.stopped = False
        self.final_manifest = None

    def close(self):
        with self.lock:
            self.stopped = True

    def media_time(self):
        return self.clock() - self.origin if self.origin is not None else 0

    def publish(self, payload):
        incoming, finalized = parse_manifest(payload)
        edge = min(track.segments[-1].ends for track in incoming.values())
        with self.lock:
            self.require_active()
            if self.final_manifest is not None:
                if payload == self.final_manifest:
                    return
                raise ConflictingFragment("The contribution has already finalized.")
            earliest = self.media_time() - TARGET_DELAY - self.window - PLAYLIST_GRACE
            if finalized and any(
                track.init_file not in self.objects or any(
                    segment.ends >= earliest and track.filename(segment.sequence) not in self.objects
                    for segment in track.segments)
                for track in incoming.values()
            ):
                raise ConflictingFragment("Upload the final media fragments before finalizing the contribution.")
            if not self.tracks:
                self.origin = self.clock() - edge
                self.starts_at = time() + TARGET_DELAY - edge
            elif incoming.keys() != self.tracks.keys():
                raise ConflictingFragment("The active contribution cannot change representation IDs.")
            tracks = {}
            for identity, track in incoming.items():
                prior = self.tracks.get(identity)
                timeline = {segment.sequence: segment for segment in prior.segments if segment.ends >= earliest} if prior else {}
                if prior and (track.codec, track.resolution) != (prior.codec, prior.resolution):
                    raise ConflictingFragment("The active contribution cannot change a track's codec or dimensions.")
                for segment in track.segments:
                    previous = timeline.get(segment.sequence)
                    if previous is not None and previous != segment:
                        raise ConflictingFragment("The published DASH timeline cannot shift an existing fragment.")
                    if segment.ends >= earliest:
                        timeline[segment.sequence] = segment
                latest = sorted(timeline.values(), key=lambda segment: segment.sequence)[-MAX_SEGMENTS:]
                if any(item.opens < previous.opens for previous, item in zip(latest, latest[1:])):
                    raise ConflictingFragment("DASH fragments must follow their track's presentation order.")
                tracks[identity] = replace(track, segments=tuple(latest))
            self.tracks = tracks
            if finalized:
                self.final_manifest = payload
            self.expire_media(earliest)

    def put(self, filename, payload):
        initialize = INIT_FILE.fullmatch(filename)
        segment = MEDIA_FILE.fullmatch(filename)
        if not initialize and not segment:
            raise ValueError("Use a numbered DASH fragment name.")
        boxes = (b"ftyp",) if initialize else (b"styp", b"moof", b"sidx")
        if not 8 <= len(payload) <= MAX_FRAGMENT_BYTES or payload[4:8] not in boxes:
            raise ValueError("The asset is outside the supported fragmented-MP4 profile.")
        track_id = (initialize or segment).group(1)
        with self.lock:
            self.require_active()
            track = self.tracks.get(track_id)
            if self.tracks and track is None:
                raise ConflictingFragment("The fragment does not belong to an active track.")
            if segment and track and track.segments:
                sequence = int(segment.group(2))
                if not track.segments[0].sequence <= sequence <= track.segments[-1].sequence + MAX_FUTURE_FRAGMENTS:
                    raise ConflictingFragment("The fragment is outside the contribution window.")
            if filename in self.objects:
                if (self.path / filename).read_bytes() == payload:
                    return False
                raise ConflictingFragment("The committed fragment has different bytes.")
            if self.final_manifest is not None:
                raise ConflictingFragment("The contribution has already finalized.")
            if self.bytes + len(payload) > MAX_SPOOL_BYTES or len(self.objects) >= 4000:
                raise FullBuffer("The occurrence's contribution spool is full.")
            (self.path / filename).write_bytes(payload)
            self.objects[filename] = len(payload), self.clock()
            self.bytes += len(payload)
            return True

    def require_active(self):
        if self.stopped:
            raise FileNotFoundError("This contribution has closed.")

    def expire_media(self, earliest):
        keep = {track.init_file for track in self.tracks.values()}
        for track in self.tracks.values():
            keep.update(track.filename(fragment.sequence) for fragment in track.segments if fragment.ends >= earliest)
        now = self.clock()
        for filename, (size, arrival) in list(self.objects.items()):
            if filename not in keep and now - arrival > PLAYLIST_GRACE:
                try:
                    (self.path / filename).unlink(missing_ok=True)
                except PermissionError:
                    continue
                self.bytes -= size
                del self.objects[filename]

    def ready_tracks(self, keep_grace=False):
        available = self.media_time() - TARGET_DELAY
        earliest = available - self.window - (PLAYLIST_GRACE if keep_grace else 0)
        outputs = {}
        for identity, track in self.tracks.items():
            if track.init_file not in self.objects:
                continue
            window = []
            for segment in track.segments:
                if segment.ends <= earliest:
                    continue
                if segment.ends > available or track.filename(segment.sequence) not in self.objects:
                    break
                if window and segment.sequence != window[-1].sequence + 1:
                    break
                window.append(segment)
            if len(window) >= 2:
                outputs[identity] = track, window
        return outputs

    def playable(self, outputs):
        visible = [track for track, _ in outputs.values()]
        needs_audio = any(track.resolution is None for track in self.tracks.values())
        return any(track.resolution for track in visible) and (not needs_audio or any(track.resolution is None for track in visible))

    def live(self):
        with self.lock:
            return not self.stopped and self.playable(self.ready_tracks())

    def drained(self):
        available = self.media_time() - TARGET_DELAY
        return self.final_manifest is not None and all(
            not track.segments or track.segments[-1].ends <= available for track in self.tracks.values())

    def audience_finished(self):
        with self.lock:
            return not self.stopped and self.drained()

    def relay_playlists(self):
        with self.lock:
            outputs = self.ready_tracks()
            if self.stopped or not self.playable(outputs):
                return {}
            ended = self.drained()
            tracks = {identity: track for identity, (track, _) in outputs.items()}
            return {"relay-master.m3u8": master_playlist(tracks, "relay-")} | {
                f"relay-{identity}.m3u8": media_playlist(track, window, ended, self.starts_at)
                for identity, (track, window) in outputs.items()
            }

    def resource(self, filename):
        with self.lock:
            self.require_active()
            outputs = self.ready_tracks()
            if not self.playable(outputs):
                raise FileNotFoundError("The contribution has not filled the initial buffer.")
            if filename == "master.m3u8":
                return master_playlist({identity: track for identity, (track, _) in outputs.items()})
            if filename.endswith(".m3u8") and filename[:-5] in outputs:
                track, window = outputs[filename[:-5]]
                return media_playlist(track, window, self.drained(), self.starts_at)
            for track, window in self.ready_tracks(keep_grace=True).values():
                if filename == track.init_file or any(filename == track.filename(segment.sequence) for segment in window):
                    return self.path / filename
            raise FileNotFoundError("The fragment is outside this event's playback window.")
