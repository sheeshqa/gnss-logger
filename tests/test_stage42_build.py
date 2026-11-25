"""Build orchestration tests; PyInstaller and hardware are never launched."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from tools.build_gui_stage4_2 import build_commands, main


class BuildTests(unittest.TestCase):
    def test_dry_run_creates_nothing(self):
        with TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            root = Path(directory)
            self.assertEqual(main(['--output-root', str(root), '--build-id', 'dry', '--dry-run']), 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_path_escape_id_rejected(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(['--build-id', '../escape', '--dry-run'])

    def test_existing_run_is_never_overwritten(self):
        with TemporaryDirectory() as directory, redirect_stderr(StringIO()):
            marker = Path(directory) / 'previous'
            marker.mkdir()
            original = marker / 'original.txt'
            original.write_text('unchanged', encoding='utf-8')
            with self.assertRaises(SystemExit):
                main(['--output-root', directory, '--build-id', 'previous'])
            self.assertEqual(original.read_text(encoding='utf-8'), 'unchanged')

    def test_command_vectors_keep_gui_windowed_cli_console(self):
        commands = build_commands(Path('synthetic-build'))
        self.assertIn('--windowed', commands[0])
        self.assertIn('--console', commands[1])
        self.assertTrue(all('--noconfirm' not in command for command in commands))
        self.assertIn('--console', build_commands(Path('test'), gui_console=True)[0])

    def test_build_failure_preserves_new_run_and_does_not_run_cli(self):
        with TemporaryDirectory() as directory, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with patch('tools.build_gui_stage4_2.subprocess.run', return_value=Mock(returncode=9)) as invoke:
                self.assertEqual(main(['--output-root', directory, '--build-id', 'failed']), 9)
            self.assertEqual(invoke.call_count, 1)
            self.assertFalse(invoke.call_args.kwargs['shell'])
            self.assertTrue((Path(directory) / 'failed' / 'commands.json').is_file())


if __name__ == '__main__':
    unittest.main()
