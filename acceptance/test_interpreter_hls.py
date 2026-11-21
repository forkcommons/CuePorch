"""Keep video and interpreter audio synchronized as encoder changes roll off."""

import unittest

from event_audio import timed_fragments
from event_interpreter_timing import paired_playlists


class InterpreterPlaylistTest(unittest.TestCase):
    def test_rolling_window_preserves_encoder_discontinuity_numbers(self):
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:10",
                 "#EXT-X-DISCONTINUITY-SEQUENCE:4"]
        for number in range(10, 14):
            if number in (11, 13):
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend((f"#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:{number:02d}Z",
                          "#EXTINF:1.0,", f"0-{number:09d}.ts"))
        source = "\n".join(lines) + "\n"
        fragments = list(timed_fragments(source))
        for selected, base, boundary in ((fragments[1:3], 4, 11), (fragments[2:], 5, 13)):
            _, video, audio = paired_playlists(source, selected, "es", "Spanish")
            self.assertEqual(video, audio.replace("\n1-", "\n0-"))
            self.assertIn(f"#EXT-X-DISCONTINUITY-SEQUENCE:{base}\n", video)
            self.assertEqual(video.splitlines().count("#EXT-X-DISCONTINUITY"), 1)
            self.assertIn(f"#EXT-X-DISCONTINUITY\n#EXTINF:1.000000,\n0-{boundary:09d}.ts\n", video)
