import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

from gnss_logger.gui.preferences import (ConflictError, PreferencesStore,
    default_preferences, validate_preferences)


class PreferencesTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'config/preferences.json'
        self.defaults = default_preferences(self.root, mock=True)
        self.store = PreferencesStore(self.path, self.defaults)

    def test_absent_load_no_writes_and_partial_roundtrip(self):
        self.assertEqual(self.store.load(), self.defaults)
        self.assertFalse(self.path.parent.exists())
        value = self.store.save({'interface': {'page': 2}})
        self.assertEqual(value['interface']['page'], 2)
        self.assertEqual(PreferencesStore(self.path, self.defaults).load(), value)
        self.assertEqual(value['recording']['output'], str(self.root/'internal/runs'))

    def test_return_values_are_independent(self):
        value = self.store.load()
        value['history']['recording_roots'].clear()
        self.assertTrue(self.store.load()['history']['recording_roots'])

    def test_corrupt_json_preserved_and_explicit_recovery_backed_up(self):
        for raw in (b'{broken', b'\xff', b'[]', b'{"schema_version":2}',
                    b'{"schema_version":1,"schema_version":1}',
                    b'{"schema_version":1,"device_identity":{}}'):
            with self.subTest(raw=raw):
                self.path.parent.mkdir(exist_ok=True)
                self.path.write_bytes(raw)
                store = PreferencesStore(self.path, self.defaults)
                self.assertEqual(store.load(), self.defaults)
                self.assertTrue(store.warnings)
                with self.assertRaises(ValueError):
                    store.save(self.defaults)
                self.assertEqual(self.path.read_bytes(), raw)
                store.save(self.defaults, recover=True)
                self.assertIn(raw, [p.read_bytes() for p in self.path.parent.glob('*.bak')])
                self.assertEqual(json.loads(self.path.read_text()), self.defaults)

    def test_stored_version_required_partial_fields_allowed(self):
        self.path.parent.mkdir()
        self.path.write_text('{"schema_version":1,"recording":{"baud":9600}}')
        self.assertEqual(self.store.load()['recording']['baud'], 9600)
        self.path.write_text('{}')
        self.store.load()
        self.assertTrue(self.store.warnings)
        with self.assertRaises(ValueError):
            self.store.save(self.defaults)

    def test_invalid_fields(self):
        invalid = [dict(schema_version=True), dict(device={}),
            dict(recording=dict(output='relative')), dict(recording=dict(baud=True)),
            dict(recording=dict(expected_hz=float('nan'))),
            dict(recording=dict(seconds=0)), dict(recording=dict(station='CON')),
            dict(recording=dict(retention_policy='delete')),
            dict(recording=dict(auto_enabled=True)),
            dict(recording=dict(daily_root=str(self.root/'internal'))),
            dict(conversion=dict(config_path='')), dict(conversion=dict(timeout_s=1)),
            dict(history=dict(recording_roots='bad')),
            dict(interface=dict(polling_ms=True)), dict(interface=dict(page=4)),
            dict(interface=dict(geometry={'x':0}))]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                validate_preferences(value, self.defaults)

    def test_supplied_defaults_cannot_redefine_schema_or_types(self):
        for defaults in (dict(schema_version=2), dict(schema_version=True),
                         dict(device_identity={}), dict(recording=[]),
                         dict(recording=dict(baud=True)), [], False):
            with self.subTest(defaults=defaults), self.assertRaises(ValueError):
                validate_preferences({}, defaults)
            with self.subTest(store_defaults=defaults), self.assertRaises(ValueError):
                PreferencesStore(self.path, defaults)

    def test_stored_numeric_edge_cases_preserved(self):
        self.path.parent.mkdir()
        cases = [(key, value) for key in ('seconds', 'expected_hz')
                 for value in (True, False, float('nan'), float('inf'), 10**400)]
        cases.extend([('baud', True), ('baud', float('nan'))])
        for key, value in cases:
            with self.subTest(key=key, value=value):
                values = default_preferences(self.root, mock=True)
                values['recording'][key] = value
                raw = json.dumps(values).encode('utf-8')
                self.path.write_bytes(raw)
                self.assertEqual(self.store.load(), self.defaults)
                self.assertTrue(self.store.warnings)
                with self.assertRaises(ValueError):
                    self.store.save(self.defaults)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_geometry_unicode_and_manual_duration(self):
        self.defaults['interface']['geometry'] = dict(x=-100, y=0, width=1100, height=750, maximized=True)
        self.defaults['recording'].update(seconds=None, output=str(self.root/'測定😀'))
        self.store.save(self.defaults)
        self.assertEqual(self.store.load(), self.defaults)

    def test_conflicting_stores_and_lock(self):
        self.store.save(self.defaults)
        other = PreferencesStore(self.path, self.defaults)
        other.load()
        self.store.save({'interface': {'page':1}})
        before = self.path.read_bytes()
        with self.assertRaises(ConflictError):
            other.save(self.defaults)
        self.assertEqual(self.path.read_bytes(), before)
        lock = self.path.with_name(self.path.name+'.lock')
        lock.touch()
        with self.assertRaises(ConflictError):
            self.store.save(self.defaults)
        self.assertTrue(lock.exists())

    def test_simultaneous_writers_one_revision_wins(self):
        self.store.save(self.defaults)
        stores = [PreferencesStore(self.path, self.defaults) for _ in range(2)]
        for store in stores:
            store.load()
        barrier = Barrier(2)
        def save(index):
            barrier.wait(timeout=5)
            try:
                stores[index].save({'interface': {'page': index+1}})
                return True
            except ConflictError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.load()['interface']['page'], results.index(True)+1)

    def test_write_errors_preserve_last_valid_and_cleanup(self):
        self.store.save(self.defaults)
        before, revision = self.path.read_bytes(), self.store.fingerprint
        for target in ('os.replace', 'os.fsync', 'tempfile.NamedTemporaryFile'):
            with self.subTest(target=target), patch('gnss_logger.gui.preferences.'+target,
                                                   side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    self.store.save({'interface': {'page':1}})
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.store.fingerprint, revision)
            self.assertFalse(list(self.path.parent.glob('*.tmp')))
            self.assertFalse(list(self.path.parent.glob('*.lock')))

    def test_read_access_failure_blocks_save(self):
        self.store.save(self.defaults)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('denied')):
            self.assertEqual(self.store.load(), self.defaults)
            self.assertTrue(self.store.warnings)
        with self.assertRaises(ValueError):
            self.store.save(self.defaults)

    def test_mock_defaults_never_consult_production_root(self):
        with patch('gnss_logger.gui.preferences.default_root', side_effect=AssertionError):
            self.assertIn('GNSS-GUI-MOCK', default_preferences(mock=True)['recording']['output'])
            self.assertEqual(default_preferences(self.root, mock=True), self.defaults)
