"""Frame rate badge: message parsing, the median, the corner drawing and how often it costs a write."""

import os
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

from pixelface import fps, render, service  # noqa: E402
from pixelface.settings import DEFAULTS, validate  # noqa: E402
from test_service import FakeLink  # noqa: E402

FRAME = 64 * 54 * 3


NONE = fps.NO_VALUE


def body(frametime_ns, steam=False, refresh=60, visible_ns=NONE):
    """A mangoapp_msg_v1 body as gamescope packs it (without the C long mtype)."""
    return (struct.pack("<IIQBBQQIIH", 1, 1234, frametime_ns, 0, 0, visible_ns, NONE, 1920, 1080, refresh)
            + bytes([0x02 if steam else 0]) + b"gamescope".ljust(40, b"\0") + b"\0\0")


class ParseTest(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(fps.parse(body(16_666_667)), (16_666_667, None, 60, False))
        self.assertEqual(fps.parse(body(NONE, visible_ns=33_333_333)), (None, 33_333_333, 60, False))
        self.assertTrue(fps.parse(body(1, steam=True))[3])
        self.assertIsNone(fps.parse(b"\x01\x00"))

    def test_frame_rate_is_the_median(self):
        times = [16_666_667] * 20 + [200_000_000]  # one hitch
        self.assertEqual(fps.frame_rate(times), 60)
        self.assertEqual(fps.frame_rate([33_333_333] * 10), 30)
        self.assertIsNone(fps.frame_rate([16_666_667] * 3))
        self.assertIsNone(fps.frame_rate([2 ** 64 - 1] * 10))  # "no value" markers

    def test_no_gamescope_is_no_reading(self):
        with tempfile.TemporaryDirectory() as proc:
            libc = mock.Mock()
            libc.ftok.return_value = -1
            libc.msgget.return_value = -1
            self.assertIsNone(fps.MangoappFeed(libc=libc, proc_root=proc).sample(0.01))


class OverlayTest(unittest.TestCase):
    def test_draws_only_in_the_corner(self):
        art = bytes([200]) * FRAME
        out = render.overlay_fps(art, 60)
        self.assertNotEqual(out, art)
        self.assertEqual(out[-3 * 64:], art[-3 * 64:])  # bottom row untouched
        self.assertEqual(out[3 * 63:3 * 64], art[3 * 63:3 * 64])  # top right untouched
        self.assertEqual(out[0], 200 // 5)  # box behind the digits is darkened
        self.assertIs(render.overlay_fps(art, None), art)

    def test_settings(self):
        self.assertFalse(DEFAULTS["fps_overlay"])
        self.assertEqual(validate({"fps_interval": 120}), {"fps_interval": 120})
        with self.assertRaises(ValueError):
            validate({"fps_interval": 5})


class FakeFeed:
    def __init__(self):
        self.readings = []

    def sample(self, seconds):
        return self.readings.pop(0) if self.readings else None


@mock.patch.object(service, "find_port", lambda: "/dev/ttyUSB0")
@mock.patch.object(service, "MIN_UPLOAD_GAP", 0)
@mock.patch.object(render, "running_appid", lambda: 0)
@mock.patch.object(render, "artwork", lambda appid, **_: bytes([90]) * FRAME)
class ServiceFpsTest(unittest.TestCase):
    def setUp(self):
        FakeLink.instances = []
        self.feed = FakeFeed()
        cfg = dict(DEFAULTS, mode="artwork", fps_overlay=True, fps_interval=60)
        self.svc = service.FaceplateService(cfg, link_factory=FakeLink, fps_feed=self.feed)

    def reading(self, value):
        self.feed.readings.append(value)
        self.svc.fps.next_at = time.monotonic() - 1
        self.svc.fps.step()
        self.svc.step()

    def test_small_changes_cost_no_writes(self):
        self.svc.game_event(570, True)
        self.svc.step()  # art without a number yet
        self.reading(60)  # number appears
        self.reading(59)  # within FPS_STEP: same picture, no write
        self.reading(61)
        self.assertEqual(self.svc.fps_shown, 60)
        self.reading(45)  # a real drop is drawn
        self.assertEqual(self.svc.fps_shown, 45)
        self.assertEqual(len(FakeLink.instances[0].gifs), 3)

    def test_first_reading_waits_and_new_game_starts_over(self):
        self.svc.game_event(570, True)
        self.svc.step()
        self.feed.readings.append(60)
        self.svc.fps.step()  # too early after the launch: nothing measured
        self.assertIsNone(self.svc.fps.value)
        self.reading(60)
        self.svc.game_event(570, False)
        self.svc.game_event(730, True)
        self.svc.step()
        self.assertIsNone(self.svc.fps_shown)

    def test_turning_it_off_stops_measuring(self):
        self.svc.game_event(570, True)
        self.svc.step()
        self.reading(60)
        self.svc.configure(dict(self.svc.cfg, fps_overlay=False))
        self.svc.step()
        self.assertFalse(self.svc.fps.enabled)
        self.assertIsNone(self.svc.fps_shown)


class DownloadTest(unittest.TestCase):
    def test_fetches_once_and_remembers_misses(self):
        jpeg = b"\xff\xd8\xff" + b"x" * 100
        calls = []

        def fetch(url):
            calls.append(url)
            return jpeg if "library_hero" in url else None

        with tempfile.TemporaryDirectory() as folder:
            path = render.downloaded_art(292030, "hero", folder, fetch)
            self.assertTrue(path.endswith("292030_hero.jpg"))
            self.assertEqual(render.downloaded_art(292030, "hero", folder, fetch), path)
            self.assertIsNone(render.downloaded_art(292030, "logo", folder, fetch))
            before = len(calls)
            self.assertIsNone(render.downloaded_art(292030, "logo", folder, fetch))
            self.assertEqual(len(calls), before)  # the miss is remembered
            self.assertIsNone(render.downloaded_art(3045812345, "hero", folder, fetch))  # shortcut


if __name__ == "__main__":
    unittest.main()
