"""Check live ring retention and a bounded recording failure."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from event_replay_spool import reserve


def write_source(directory, first):
    directory.mkdir(exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1",
             f"#EXT-X-MEDIA-SEQUENCE:{first}",
             f"#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:{first:02d}.000Z"]
    for index in range(first, first + 2):
        name = f"0-{index:09d}.ts"
        (directory / name).write_bytes(bytes((index,)) * 512)
        lines.extend(("#EXTINF:1.0,", name))
    (directory / "0.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")


class InterpretedReplaySpoolTest(unittest.TestCase):
    def test_keeps_program_before_ring_expires_and_detects_missing_segment(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            spool = reserve(root / "replays", "event", 123, "remote")
            try:
                write_source(source, 0)
                spool.collect(source)
                original = source / "0-000000000.ts"
                retained = spool.directory / original.name
                self.assertTrue(retained.samefile(original))
                original.unlink()
                self.assertEqual(retained.read_bytes(), bytes(512))
                write_source(source, 2)
                spool.collect(source)
                self.assertEqual(spool.latest, 3)
                write_source(source, 5)
                spool.collect(source)
                self.assertTrue(spool.failed)
            finally:
                spool.finish()
            self.assertFalse(spool.directory.exists())

    def test_recording_budget_exhaustion_leaves_the_live_segment_in_place(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            spool = reserve(root / "replays", "event", 123, "remote")
            try:
                spool.byte_limit = 511
                write_source(source, 0)
                spool.collect(source)
                self.assertTrue(spool.failed)
                self.assertEqual(spool.size, 0)
                self.assertTrue((source / "0-000000000.ts").is_file())
            finally:
                spool.finish()
            self.assertFalse(spool.directory.exists())
