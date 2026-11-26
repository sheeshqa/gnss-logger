"""Focused packaging safety contracts; no GUI/core suite."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage45build', ROOT/'tools/build_stage45d.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

class BuildTests(unittest.TestCase):
    def test_reuse_rejects_corrupted_frozen_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = root/'dist/GNSSLogger'
            payload.mkdir(parents=True)
            (root/'BUILD_COMPLETE.txt').write_text('complete')
            for name in ('gnss-logger.exe', 'gnss-logger-gui.exe'):
                (payload/name).write_bytes(b'original')
            prior = {'source_sha256': {}, 'files': build.inventory(payload)}
            (payload/'build-manifest.json').write_text(json.dumps(prior))
            (payload/'gnss-logger.exe').write_bytes(b'corrupted')
            with patch.object(build, 'sources', return_value={}):
                with self.assertRaisesRegex(RuntimeError, 'payload hash/size mismatch'):
                    build.reuse_frozen(root, root/'new')

    def test_reuse_rejects_changed_application_source(self):
        prior = {'source_sha256': {'gnss_logger/gui/app.py': 'old'}}
        with patch.object(build, 'sources', return_value={'gnss_logger/gui/app.py': 'new'}):
            with self.assertRaisesRegex(RuntimeError, 'full freeze required'):
                build.verify_freeze_inputs(prior)

    def test_changed_source_rejects_and_records_relative_difference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(build, 'sources', return_value={'gnss_logger/gui/app.py': 'new'}):
                with self.assertRaisesRegex(RuntimeError, 'reject this artifact'):
                    build.verify_sources({'gnss_logger/gui/app.py': 'old'}, root)
            record = (root/'source-verification.json').read_text(encoding='utf-8')
            self.assertIn('"matched": false', record)
            self.assertIn('gnss_logger/gui/app.py', record)
            self.assertNotIn(str(ROOT), record)

    def test_inventory_uses_relative_paths_and_real_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'example').write_bytes(b'abc')
            self.assertEqual(build.inventory(root), {'example': {'bytes': 3, 'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'}})

    def test_installer_preserves_external_data_and_does_not_kill(self):
        text = (ROOT/'packaging/installer.iss').read_text(encoding='utf-8')
        for value in ('PrivilegesRequired=lowest', 'CloseApplications=no', 'RestartApplications=no',
                      "CheckForMutexes('Local\\GNSSLogger.GUI,Local\\GNSSLogger.CLI')", 'function InitializeUninstall'):
            self.assertIn(value, text)
        self.assertNotIn('[UninstallDelete]', text)
        self.assertNotIn('[InstallDelete]', text)
        self.assertEqual(text.count('SuppressibleMsgBox('), 2)
        self.assertIn('DefaultGroupName={#AppGroup}', text)

    def test_shared_payload_and_console_modes(self):
        text = (ROOT/'packaging/gnss.spec').read_text(encoding='utf-8')
        self.assertEqual(text.count('COLLECT('), 1)
        self.assertIn('console=False', text)
        self.assertIn('console=True', text)
        self.assertNotIn('convbin', text)

if __name__ == '__main__':
    unittest.main()
