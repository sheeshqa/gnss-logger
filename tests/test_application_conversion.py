"""Application conversion requests and adapters, without receiver hardware."""
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import test_stage31_conversion as fixtures
from gnss_logger.conversion import cli, service
from gnss_logger.conversion.job import ConversionJob
from gnss_logger.conversion.selection import Source
from gnss_logger.conversion.worker import SegmentWorker


class ConversionRequestTests(TestCase):
    def test_request_rejects_invalid_path_and_flag_types(self):
        for values in ({'config':'settings.toml'},{'output':123},{'input':[]},
                       {'dry_run':'yes'},{'allow_obs_without_nav':1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                service.ConversionRequest(**values)

    def test_defaults_frozen_and_legacy_adapter(self):
        request = service.ConversionRequest()
        self.assertEqual(request, service.ConversionRequest.from_args(None))
        with self.assertRaises(FrozenInstanceError):
            request.dry_run = True
        legacy = SimpleNamespace(config=Path('settings.toml'), dry_run=True)
        adapted = service.ConversionRequest.from_args(legacy)
        legacy.dry_run = False
        self.assertTrue(adapted.dry_run)
        self.assertEqual(adapted.config, Path('settings.toml'))
        self.assertIsNone(adapted.input)
        self.assertFalse(adapted.allow_obs_without_nav)

    def test_cli_passes_all_request_fields_and_execution_hooks(self):
        args = SimpleNamespace(config=Path('config'), output=Path('output'),
            input=Path('input'), run_id='run', dry_run=True, allow_obs_without_nav=True)
        sources, settings, cancel, progress = [], object(), Event(), lambda report: None
        with patch.object(service, 'execute', return_value={'exit_code': 3}) as execute:
            self.assertEqual(cli.execute(args, sources, settings=settings,
                cancel=cancel, progress=progress), {'exit_code': 3})
        execute.assert_called_once_with(service.ConversionRequest(**vars(args)), sources,
            settings=settings, cancel=cancel, progress=progress)

    def test_job_adapts_legacy_keyword_without_retaining_mutable_args(self):
        args = SimpleNamespace(dry_run=True)
        job = ConversionJob(args=args)
        args.dry_run = False
        with patch('gnss_logger.conversion.job.execute',
                   return_value={'status': 'DRY_RUN', 'exit_code': 0}) as execute:
            job.start()
            self.assertEqual(job.result(wait=True, timeout=3)['exit_code'], 0)
        self.assertIsInstance(execute.call_args.args[0], service.ConversionRequest)
        self.assertTrue(execute.call_args.args[0].dry_run)
        self.assertEqual(ConversionJob().request, service.ConversionRequest())

    def test_worker_uses_neutral_request(self):
        with patch('gnss_logger.conversion.worker.Thread'):
            worker = SegmentWorker(Path('settings.toml'))
        self.assertEqual(worker.request, service.ConversionRequest(config=Path('settings.toml')))


class ApplicationConversionTests(TestCase):
    setUp = fixtures.ConversionTests.setUp
    subprocess_fixture = fixtures.ConversionTests.subprocess_fixture
    popen_fixture = fixtures.ConversionTests.popen_fixture

    def test_direct_service_converts_and_publishes_wire_report(self):
        request = service.ConversionRequest(config=self.config)
        with patch('gnss_logger.conversion.engine.subprocess.run', self.subprocess_fixture):
            report = service.execute(request, [Source(self.source, utc_day='2026-11-16')])
        self.assertEqual(report['exit_code'], 0)
        self.assertIn(report['status'], ('SUCCESS', 'SUCCESS_WITH_WARNINGS'))
        self.assertEqual(report['schema_version'], 2)
        self.assertEqual(report['schema_id'], 'gnss_logger.conversion')
        self.assertTrue(Path(report['report_path']).is_file())
        self.assertTrue(report['segments'][0]['published_outputs'])

    def test_cli_and_service_cancelled_wire_reports_match(self):
        request = service.ConversionRequest(config=self.config, dry_run=True)
        cancel = Event()
        cancel.set()
        with patch.object(service.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixed')), \
             patch.object(service, 'utc_now', return_value='fixed-time'):
            direct = service.execute(request, [Source(self.source)], cancel=cancel)
            adapted = cli.execute(SimpleNamespace(config=self.config, dry_run=True),
                                  [Source(self.source)], cancel=cancel)
        self.assertEqual(direct, adapted)
        self.assertEqual((direct['status'], direct['exit_code']), ('INTERRUPTED', 130))
        self.assertEqual(direct['segments'], [])
