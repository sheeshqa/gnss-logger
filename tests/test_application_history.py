"""Read-only history over temporary metadata; no COM or converter processes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from gnss_logger.application.history import HistoryService
import test_stage23_projection as projection_fixtures
import test_stage2_record as recording_fixtures


class HistoryTests(TestCase):
    midnight = projection_fixtures.ProjectionTests.midnight
    run_record = projection_fixtures.ProjectionTests.run_record

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / 'internal' / 'runs'
        self.converted = self.root / 'converted'
        self.stage1 = self.root / 'stage1'
        for path in (self.runs, self.converted, self.stage1):
            path.mkdir(parents=True)
        self.history = HistoryService([self.runs], [self.converted], [self.stage1])

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def old_session(self, name='one', manifest=True):
        directory = self.stage1 / name
        capture = self.root / (name + '.ubx')
        capture.write_bytes(b'original')
        value = dict(schema_version=1, session_id=name, started_utc='2026-10-27T00:00:00Z',
                     ended_utc='2026-10-27T00:00:01Z', exit_code=0,
                     registration='STOPPED_COMPLETE', artifacts=[dict(path=str(capture),
                     bytes=8, sha256='a' * 64, complete=True, status='closed_complete')])
        self.write(directory / ('manifest.json' if manifest else 'report.json'), value)
        return directory, value

    def operation(self, source, name='op', status='SUCCESS', outputs=None):
        segment = dict(source=source, status=status, published_outputs=outputs or {},
                       validation={}, errors=[], warnings=[])
        value = dict(schema_id='gnss_logger.conversion', schema_version=2,
                     operation_id=name, status=status, segments=[segment])
        path = self.converted / name / ('conversion-' + name + '.json')
        self.write(path, value)
        return path, value

    def test_stage1_report_only_is_incomplete_and_queries_do_not_hash_or_write(self):
        directory, value = self.old_session(manifest=False)
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('hashlib.sha256', side_effect=AssertionError('history hashed')), \
                patch('gnss_logger.session.file_fingerprint', side_effect=AssertionError('history fingerprinted')):
            session = self.history.list_sessions()[0]
            self.assertFalse(session['terminal'])
            self.assertEqual(session['state'], 'INCOMPLETE')
            self.assertFalse(session['independently_verified'])
            self.assertEqual(session['report'], value)
            self.assertTrue(session['provisional'])
            self.assertEqual(len(session['ubx_files']), 1)
            self.assertEqual(session['duration_s'], 1)
            self.assertEqual(self.history.get_session(str(directory)), session)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_stage1_fingerprint_multiple_conversions_missing_outputs_and_standalone(self):
        directory, manifest = self.old_session()
        source = manifest['artifacts'][0].copy()
        source['path'] = str(self.root / 'explicit-renamed.ubx')
        self.operation(source, 'one', 'OBS_ONLY_INCOMPLETE_NAVIGATION', {'obs': str(self.root / 'absent.obs')})
        self.operation(source, 'two', 'SKIPPED')
        self.operation(dict(path=str(self.root / 'standalone.ubx')), 'standalone')
        session = self.history.get_session('one')
        self.assertTrue(session['terminal'])
        self.assertEqual(len(session['conversions']), 2)
        self.assertEqual({c['status'] for c in session['conversions']},
                         {'SKIPPED', 'OBS_ONLY_INCOMPLETE_NAVIGATION'})
        output = next(c for c in session['conversions'] if c['status'].startswith('OBS'))['segments'][0]['published_outputs']['obs']
        self.assertFalse(output['present'])
        self.assertFalse(output['independently_verified'])
        conversions = self.history.list_conversions()
        self.assertEqual(next(c for c in conversions if c['operation_id'] == 'standalone')['segments'][0]['match'], 'UNLINKED')
        self.assertEqual(len(self.history.list_conversions(str(directory))), 2)
        session['manifest']['artifacts'].clear()
        self.assertEqual(len(self.history.get_session('one')['artifacts']), 1)

    def test_ambiguous_fingerprint_does_not_choose_filename(self):
        _, manifest = self.old_session('a')
        self.old_session('b')
        self.operation(manifest['artifacts'][0])
        segment = self.history.list_conversions()[0]['segments'][0]
        self.assertEqual(segment['match'], 'AMBIGUOUS')
        self.assertEqual(len(segment['candidates']), 2)
        self.assertIsNone(segment['session_key'])

    def test_stage2_projection_ownership_stronger_than_hash_and_missing_files(self):
        controller, _, manifest, _, _ = self.run_record(root=self.root)
        artifact_dir = Path(manifest['projection']['artifact_directory'])
        artifact = next(json.loads(p.read_text()) for p in artifact_dir.glob('*.json')
                        if not p.name.endswith(('.open.json', '.failed.json')))
        source = {k: artifact[k] for k in ('path', 'run_id', 'attempt_id', 'artifact_id', 'sha256')}
        source['size'] = artifact['bytes']
        self.operation(source, 'good')
        bad = dict(source, attempt_id='f' * 32)
        self.operation(bad, 'conflict')
        self.operation(dict(source, sha256='b' * 64), 'fingerprint-conflict')
        Path(artifact['path']).unlink()
        with patch('hashlib.sha256', side_effect=AssertionError('history hashed')):
            session = self.history.get_session(manifest['attempt_id'])
            conversions = {c['operation_id']: c for c in self.history.list_conversions()}
        self.assertEqual(session['recorded_integrity'], 'VERIFIED')
        self.assertTrue(session['terminal'])
        self.assertTrue(any(not a['present'] for a in session['artifacts']))
        self.assertEqual(conversions['good']['segments'][0]['match'], 'MATCHED')
        self.assertEqual(conversions['conflict']['segments'][0]['match'], 'OWNERSHIP_CONFLICT')
        self.assertEqual(conversions['fingerprint-conflict']['segments'][0]['match'], 'FINGERPRINT_CONFLICT')
        self.assertEqual(len(session['conversions']), 1)

    def test_start_only_and_projection_directory_escape(self):
        controller, _, manifest, _, _ = self.run_record(root=self.root)
        (controller.directory / 'manifest.json').unlink()
        session = self.history.list_sessions()[0]
        self.assertFalse(session['terminal'])
        self.assertEqual(session['state'], 'INCOMPLETE')
        manifest['projection']['artifact_directory'] = str(self.root / 'outside')
        self.write(controller.directory / 'manifest.json', manifest)
        session = self.history.list_sessions()[0]
        self.assertTrue(any('escapes recording attempt' in d['message'] for d in session['diagnostics']))
        self.assertEqual(len(session['artifacts']), len(manifest['artifacts']))

    def test_malformed_shapes_and_unknown_schemas_keep_catalog_alive(self):
        self.old_session()
        source = dict(path=str(self.root / 'input.ubx'))
        _, good = self.operation(source, 'good')
        malformed = [[], None, 'text', 3, {'schema_id': [], 'schema_version': {}},
                     dict(good, schema_version=True), dict(good, schema_version=999),
                     dict(good, segments={}), dict(good, segments=[None])]
        for key, value in [('source', []), ('source', {'path': []}),
                           ('published_outputs', []), ('published_outputs', {'obs': []}),
                           ('validation', []), ('warnings', {}), ('missing_outputs', [{}])]:
            row = deepcopy(good)
            row['segments'][0][key] = value
            malformed.append(row)
        for index, value in enumerate(malformed):
            self.write(self.converted / f'bad-{index}.json', value)
        for index, value in enumerate(([], {'schema_version': 1, 'session_id': 'bad', 'artifacts': {}},
                                       {'schema_version': 1, 'session_id': 'bad', 'artifacts': [None]})):
            self.write(self.stage1 / f'bad-{index}' / 'manifest.json', value)
        (self.converted / 'broken.json').write_text('{', encoding='utf-8')
        self.assertEqual(len(self.history.list_conversions()), 1)
        self.assertGreaterEqual(len(self.history.diagnostics), len(malformed) + 4)
        self.assertEqual(len(self.history.list_sessions()), 4)

    def test_receipt_schema_and_current_presence(self):
        _, manifest = self.old_session()
        output = self.converted / 'output.obs'
        output.write_bytes(b'OBS')
        receipt = dict(schema_id='gnss_logger.conversion.result', schema_version=1,
                       identity={'source_sha256': 'a' * 64, 'source_bytes': 8},
                       source=manifest['artifacts'][0], status='OBS_ONLY_INCOMPLETE_NAVIGATION',
                       published_outputs={'obs': 'output.obs'}, missing_outputs=['nav'])
        self.write(self.converted / 'result.json', receipt)
        conversion = self.history.list_conversions()[0]
        self.assertEqual(conversion['kind'], 'receipt')
        self.assertTrue(conversion['segments'][0]['published_outputs']['obs']['present'])
        self.assertEqual(conversion['segments'][0]['match'], 'MATCHED')
        self.assertEqual(conversion['raw']['missing_outputs'], ['nav'])

    def test_missing_roots_and_ambiguous_session_id(self):
        self.history = HistoryService([self.root / 'absent'], [])
        self.assertEqual(self.history.list_sessions(), [])
        self.assertTrue(self.history.diagnostics)
        self.history = HistoryService([], [], [self.stage1])
        _, value = self.old_session('one')
        self.write(self.stage1 / 'copy' / 'manifest.json', value)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.history.get_session('one')

    def test_stage2_v1_partial_and_normalized_summary(self):
        controller, _, _, _, manifest, _ = recording_fixtures.RecordTests.exercise(
            self, [(0.1, recording_fixtures.stream()), OSError('disconnected')])
        history = HistoryService([Path(self.tmp.name)], [])
        session = history.get_session(manifest['attempt_id'])
        self.assertEqual(session['manifest']['schema_version'], 1)
        self.assertTrue(session['terminal'])
        self.assertEqual(session['registration'], 'PARTIAL')
        self.assertEqual(session['exit_code'], 1)
        self.assertEqual(session['model'], 'ZED-F9P')
        self.assertIsNotNone(session['duration_s'])
        self.assertEqual(session['paths']['directory'], str(controller.directory))
        self.assertEqual(session['ubx_files'][0]['metadata']['state'], 'CLOSED_PARTIAL')

    def test_report_validation_and_provisional_identity(self):
        directory, value = self.old_session(manifest=False)
        value.update(schema_version=2, adapter={'model': 'ZED-X20P'},
                     identity={'software': 'fixture'}, elapsed_s=2.5,
                     registration={'status': 'SUCCESS', 'complete': True})
        self.write(directory / 'report.json', value)
        session = self.history.get_session('one')
        self.assertEqual(session['model'], 'ZED-X20P')
        self.assertEqual(session['duration_s'], 2.5)
        self.assertEqual(session['identity'], {'software': 'fixture'})
        self.assertFalse(session['terminal'])
        self.assertTrue(session['provisional'])
        for field, bad in [('schema_version', 9), ('identity', []), ('artifacts', [{}]),
                           ('elapsed_s', {}), ('registration', {'status': []})]:
            malformed = dict(value)
            malformed[field] = bad
            self.write(directory / 'report.json', malformed)
            session = self.history.list_sessions()[0]
            self.assertIsNone(session['report'])
            self.assertTrue(session['diagnostics'])

    def test_corrupt_terminal_and_projection_owner_do_not_hide_other_sessions(self):
        controller, _, manifest, _, _ = self.run_record(root=self.root)
        self.old_session()
        artifact_dir = Path(manifest['projection']['artifact_directory'])
        artifact_path = next(p for p in artifact_dir.glob('*.json')
                             if not p.name.endswith(('.open.json', '.failed.json')))
        artifact = json.loads(artifact_path.read_text())
        artifact['connection_id'] = 'f' * 32
        self.write(artifact_path, artifact)
        session = self.history.get_session(manifest['attempt_id'])
        self.assertTrue(any('ownership conflict' in d['message'] for d in session['diagnostics']))
        (controller.directory / 'manifest.json').write_text('[]', encoding='utf-8')
        session = self.history.get_session(manifest['attempt_id'])
        self.assertFalse(session['terminal'])
        self.assertTrue(session['diagnostics'])
        self.assertEqual(len(self.history.list_sessions()), 2)

    def test_diagnostics_are_detached_and_parallel_queries_are_serialized(self):
        self.old_session()
        (self.converted / 'broken.json').write_text('{', encoding='utf-8')
        self.history.list_sessions()
        diagnostics = self.history.diagnostics
        diagnostics[0]['message'] = 'modified'
        diagnostics.clear()
        self.assertNotEqual(self.history.diagnostics[0]['message'], 'modified')
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.history.list_sessions(), range(6)))
        self.assertTrue(all(len(result) == 1 for result in results))
        self.assertEqual(len(self.history.diagnostics), 1)
