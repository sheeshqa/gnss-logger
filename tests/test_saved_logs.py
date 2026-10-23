"""Integration tests on explicitly labelled saved fixtures and the user-supplied log."""

import hashlib
from pathlib import Path
import unittest

from gnss_logger.diagnostics import Diagnostics
from gnss_logger.engine import run_source
from gnss_logger.sources import ReplaySource


class SavedLogTests(unittest.TestCase):
    def test_saved_f9p_x20p_fixtures_are_explicitly_synthetic(self):
        for profile, expected in (("f9p", "ZED-F9P"), ("x20p", "ZED-X20P")):
            with self.subTest(profile=profile):
                source = ReplaySource(Path("tests/fixtures") / f"MOCK_{profile}.ubx")
                d = Diagnostics()
                run_source(source, d)
                self.assertTrue(source.simulated)
                self.assertEqual(d.identity["model"], expected)
                self.assertEqual(d.rawx_count, 3)
                self.assertEqual(d.sfrbx_count, 3)
                self.assertEqual(d.config_pages, 1)
                self.assertEqual(d.decode_errors, {})

    def test_saved_midnight_fixture(self):
        source = ReplaySource("tests/fixtures/MOCK_midnight.ubx")
        days = {chunk.utc.date().isoformat() for chunk in source}
        self.assertEqual(days, {"2026-03-05", "2026-03-06"})
        self.assertTrue(source.simulated)

    @unittest.skipUnless(Path("data/COM4___115200_260305_024016.ubx").exists(),
                         "User log is local and is not distributed with the repository")
    def test_supplied_log_regression(self):
        d = Diagnostics()
        digest = run_source(ReplaySource("data/COM4___115200_260305_024016.ubx"), d)
        self.assertEqual(digest, "4e93d1672d8248bc310335d7636fa60e7b2c4b0231c9ebb114f5797cf86a809e")
        self.assertEqual((d.rawx_count, d.sfrbx_count), (9804, 242168))
        self.assertEqual(d.identity["model"], "ZED-F9P")
        self.assertEqual(d.identity["firmware"], "HPG 1.13")
        self.assertEqual(d.identity["protocol"], "27.12")
        self.assertEqual(d.decode_errors, {})
        self.assertEqual(d.parser.bad_checksums, 0)
        self.assertEqual(len(d.parser.buffer), 0)
        self.assertEqual(d.gap_events, 0)
        self.assertEqual(d.config_pages, 19)
