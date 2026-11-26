from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

from gnss_logger.conversion.config import load
from gnss_logger.gui.preferences import ConflictError
from gnss_logger.gui.toml_writer import TomlStore, serialize, save_settings


class TomlTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'original.toml'
        self.source.write_text('[input]\nruns_root="runs"\ndaily_root="daily"\n')
        self.settings = load(self.source)
        self.path = self.root/'elsewhere/new.toml'

    def test_full_roundtrip_and_save_as_absolute_paths(self):
        result = save_settings(self.path, self.settings)
        self.assertEqual(result, self.settings)
        self.assertEqual(load(self.path), self.settings)
        text = self.path.read_text()
        for key in ('input_format', 'allow_obs_without_nav', 'signal_masks', 'receiver_model'):
            self.assertIn(key+' = ', text)
        self.assertEqual(load(self.source), self.settings)

    def test_unicode_controls_and_backslashes(self):
        settings = replace(self.settings, runs_root=self.root/'測定😀\t\n"\\')
        save_settings(self.path, settings)
        self.assertEqual(load(self.path), settings)

    def test_literal_unicode_escape_text(self):
        settings = replace(self.settings, runs_root=self.root/r'\ud83d\ude00')
        save_settings(self.path, settings)
        self.assertEqual(load(self.path), settings)

    def test_relative_paths_rejected(self):
        with self.assertRaises(ValueError):
            serialize(replace(self.settings, runs_root=Path('relative')))
        self.assertFalse(self.path.exists())

    def test_overwrite_confirmation_and_known_revision_required(self):
        store = TomlStore(self.source)
        self.assertTrue(store.warnings)
        with self.assertRaises(FileExistsError):
            store.save(self.settings)
        with self.assertRaises(ConflictError):
            store.save(self.settings, overwrite=True)
        store.load()
        changed = replace(self.settings, timeout_s=5)
        store.save(changed, overwrite=True)
        self.assertEqual(load(self.source), changed)
        save_settings(self.source, self.settings, overwrite=True,
                      expected_fingerprint=store.fingerprint)

    def test_optimistic_conflict_and_exclusive_lock(self):
        a, b = TomlStore(self.source), TomlStore(self.source)
        a.load()
        b.load()
        a.save(replace(self.settings, interval_s=2), overwrite=True)
        before = self.source.read_bytes()
        with self.assertRaises(ConflictError):
            b.save(self.settings, overwrite=True)
        self.assertEqual(self.source.read_bytes(), before)
        self.source.with_name(self.source.name+'.lock').touch()
        with self.assertRaises(ConflictError):
            a.save(self.settings, overwrite=True)

    def test_simultaneous_writers_one_revision_wins(self):
        stores = [TomlStore(self.source) for _ in range(2)]
        for store in stores:
            store.load()
        barrier = Barrier(2)
        def save(index):
            barrier.wait(timeout=5)
            try:
                stores[index].save(replace(self.settings, interval_s=index+2), overwrite=True)
                return True
            except ConflictError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(load(self.source).interval_s, results.index(True)+2)

    def test_unknown_or_corrupt_original_preserved(self):
        for text in ('[unknown]\nvalue=1', '[input]\nunknown=1', 'invalid TOML {{'):
            self.source.write_text(text)
            before = self.source.read_bytes()
            with self.assertRaises(ValueError):
                TomlStore(self.source).save(self.settings, overwrite=True)
            self.assertEqual(self.source.read_bytes(), before)

    def test_access_and_staged_validation_failure_preserve_original(self):
        store = TomlStore(self.source)
        store.load()
        before, revision = self.source.read_bytes(), store.fingerprint
        for target in ('gnss_logger.gui.preferences.os.replace',
                       'gnss_logger.gui.preferences.os.fsync'):
            with patch(target, side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    store.save(self.settings, overwrite=True)
            self.assertEqual(self.source.read_bytes(), before)
            self.assertEqual(store.fingerprint, revision)
        with patch('gnss_logger.gui.toml_writer.load', side_effect=[self.settings, replace(self.settings, interval_s=2)]):
            with self.assertRaises(ValueError):
                store.save(self.settings, overwrite=True)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.tmp')))
        self.assertFalse(list(self.root.glob('*.lock')))
