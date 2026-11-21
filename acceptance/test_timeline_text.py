import hashlib
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timeline_render import Clip, render
from timeline_text import EFFECTS, MOTIONS, normalize_text
from timeline_text_motion import prepare_text, render_text


class TimelineText(unittest.TestCase):
    def test_effects(self):
        sample = {"text": "AaBb 123", "color": "ffff00", "fill": "182629"}
        seen = {}
        for name in EFFECTS:
            with self.subTest(effect=name):
                layers = normalize_text([dict(sample, effect=name)])
                frames = []
                for phase in (0, 0.25, 0.53, 0.75, 1):
                    with render_text(layers, (320, 240), phase) as image:
                        frames.append((hashlib.sha256(image.tobytes()).digest(), image.getbbox()))
                self.assertIsNotNone(frames[-1][1], f"{name} not visible at end")
                if name in MOTIONS:
                    self.assertGreater(len({f[0] for f in frames}), 1)
                else:
                    self.assertEqual(len({f[0] for f in frames}), 1)
                seen[name] = tuple(frames)
        self.assertEqual(len(seen), 16)
        self.assertEqual(len(set(seen.values())), 16)

    def test_rendered_wipe(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            blue = root / "blue.png"
            Image.new("RGB", (320, 180), (0, 0, 255)).save(blue)

            layers = normalize_text([{"text": "Community bulletin", "effect": "wipe", "color": "ffff00"}])
            text = prepare_text(layers, (320, 180), root / "text.png")
            first = Clip(blue, 0, 1600, False, still=True, text=text)
            second = Clip(blue, 0, 500, False, still=True)
            out = root / "out.mp4"
            render(out, [first, second], (320, 180), "000000")

            def yellow_pixels(t):
                raw = subprocess.check_output([
                    "ffmpeg", "-v", "error", "-ss", str(t), "-i", str(out),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
                ])
                self.assertEqual(len(raw), 320 * 180 * 3)
                return sum(r > 160 and g > 160 and b < 100 for r, g, b in zip(raw[::3], raw[1::3], raw[2::3]))

            early = yellow_pixels(0.033)
            half = yellow_pixels(0.35)
            finish = yellow_pixels(1.3)
            next_scene = yellow_pixels(1.8)

            self.assertLess(early, half)
            self.assertLess(half, finish)
            self.assertEqual(next_scene, 0)

            self.assertLessEqual(abs(finish - yellow_pixels(1.5)), 12)
            with Image.open(text) as image:
                self.assertTrue(image.is_animated)
                self.assertFalse(image.info.get("default_image"))
                self.assertEqual(image.info["loop"], 1)
                self.assertEqual(image.info["duration"], 80)
