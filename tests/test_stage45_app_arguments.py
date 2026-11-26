"""Acceptance switches must never silently enable hardware access."""
from contextlib import redirect_stderr
from io import StringIO
import unittest

from gnss_logger.gui.app import main


class AcceptanceArgumentsTests(unittest.TestCase):
    def rejected(self, arguments):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(arguments)
        self.assertEqual(raised.exception.code, 2)

    def test_acceptance_requires_mock(self):
        self.rejected(['--acceptance-smoke', 'result.json'])

    def test_smoke_modes_are_exclusive(self):
        self.rejected(['--mock', '--smoke-test', '--acceptance-smoke', 'result.json'])

    def test_images_require_acceptance(self):
        self.rejected(['--mock', '--smoke-images', 'images'])

    def test_acceptance_keeps_mock_root_confinement(self):
        self.rejected(['--mock', '--acceptance-smoke', 'result.json', '--mock-root', 'relative'])
