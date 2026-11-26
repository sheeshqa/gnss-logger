"""GUI conversion lifecycle and real process cancellation, without hardware."""
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import time
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch

import test_stage31_conversion as fixtures
from gnss_logger.conversion import ConversionJob
from gnss_logger.conversion.cli import execute
from gnss_logger.conversion.config import load
from gnss_logger.conversion.engine import invoke, snapshot
from gnss_logger.conversion.selection import Source
from gnss_logger.conversion.worker import SegmentWorker


class GuiConversionTests(TestCase):
    setUp = fixtures.ConversionTests.setUp
    subprocess_fixture = fixtures.ConversionTests.subprocess_fixture
    popen_fixture = fixtures.ConversionTests.popen_fixture

    def args(self):
        return Namespace(config=self.config, output=None, input=None, run_id=None, dry_run=False)

    def source_list(self):
        return [Source(self.source, utc_day='2026-11-16')]

    def test_settings_replace_validation(self):
        settings = load(self.config)
        self.assertEqual(replace(settings, timeout_s=2).timeout_s, 2)
        for values in ({'timeout_s':float('nan')}, {'interval_s':0}, {'obs':False,'nav':False},
                       {'version':'4.00'}, {'receiver_model':'bad/name'}, {'systems':('J',)},
                       {'observations':('C',)}, {'masks':('GL6I',)},
                       {'nav':False,'allow_obs_without_nav':True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(settings, **values)

    def test_cancel_before_start(self):
        job = ConversionJob(self.args(), self.source_list(), settings=load(self.config))
        job.cancel()
        self.assertIsNone(job.result())
        job.start()
        report = job.result(wait=True, timeout=3)
        self.assertEqual((report['status'], report['exit_code']), ('INTERRUPTED',130))
        self.assertEqual(job.snapshot()['state'], 'CANCELLED')
        self.assertEqual(Path(report['output_directory']).parent, self.output)
        self.assertEqual(Path(report['report_path']).parent, Path(report['output_directory']))
        self.assertFalse(self.calls)
        with self.assertRaises(RuntimeError):
            job.start()

    def test_job_pending_timeout_and_detached_state(self):
        entered, release = Event(), Event()
        def slow(args, sources, **kwargs):
            kwargs['progress']({'segments':[{'status':'WORKING'}]})
            entered.set()
            release.wait(3)
            return {'status':'SUCCESS','exit_code':0,'segments':[]}
        with patch('gnss_logger.conversion.job.execute', slow):
            job = ConversionJob().start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIsNone(job.result(wait=True, timeout=.01))
                state = job.snapshot()
                state['progress']['segments'].clear()
                self.assertEqual(len(job.snapshot()['progress']['segments']), 1)
            finally:
                release.set()
            result = job.result(wait=True, timeout=3)
        result['segments'].append('foreign')
        self.assertEqual(job.result()['segments'], [])

    def test_cancel_between_segments_preserves_publication(self):
        other = self.root/'second.ubx'
        other.write_bytes(self.original)
        cancel = Event()
        with patch('gnss_logger.conversion.engine.subprocess.Popen', self.popen_fixture):
            report = execute(self.args(), self.source_list()+[Source(other)],
                             cancel=cancel, progress=lambda report: cancel.set())
        self.assertEqual(report['exit_code'],130)
        self.assertIn(report['segments'][0]['status'], ('SUCCESS','SUCCESS_WITH_WARNINGS'))
        self.assertEqual(report['segments'][1]['status'],'NOT_RUN')
        for path in report['segments'][0]['published_outputs'].values():
            self.assertTrue(Path(path).is_file())
        self.assertFalse(list(Path(report['output_directory']).glob('.convbin-*')))

    def test_cancel_after_snapshot_cleans_temp_and_preserves_prior(self):
        with patch('gnss_logger.conversion.engine.subprocess.run', self.subprocess_fixture):
            prior = execute(self.args(), self.source_list())
        contents = {p:Path(p).read_bytes() for p in prior['segments'][0]['published_outputs'].values()}
        cancel = Event()
        def copying(source, target):
            value = snapshot(source, target)
            cancel.set()
            return value
        with patch('gnss_logger.conversion.engine.snapshot', copying):
            report = execute(self.args(), self.source_list(), cancel=cancel)
        self.assertEqual(report['segments'][0]['status'],'INTERRUPTED')
        self.assertFalse(list(Path(report['output_directory']).glob('.convbin-*')))
        self.assertEqual(contents, {p:Path(p).read_bytes() for p in contents})

    def test_cancel_during_conversion_prevents_commit(self):
        cancel = Event()
        self.side_effect = cancel.set
        with patch('gnss_logger.conversion.engine.subprocess.Popen', self.popen_fixture):
            report = execute(self.args(), self.source_list(), cancel=cancel)
        self.assertEqual(report['exit_code'],130)
        self.assertEqual(report['segments'][0]['published_outputs'],{})
        self.assertFalse(list(Path(report['output_directory']).glob('.convbin-*')))

    def test_cancel_immediately_before_commit(self):
        from gnss_logger.conversion.engine import navigation
        cancel = Event()
        def validating(path):
            result = navigation(path)
            cancel.set()
            return result
        with patch('gnss_logger.conversion.engine.subprocess.Popen',self.popen_fixture), \
                patch('gnss_logger.conversion.engine.navigation',validating), \
                patch('gnss_logger.conversion.engine.commit') as commit:
            report = execute(self.args(),self.source_list(),cancel=cancel)
        commit.assert_not_called()
        self.assertEqual(report['exit_code'],130)
        self.assertFalse(list(Path(report['output_directory']).glob('.convbin-*')))

    def test_failures_and_partial_nav_are_not_successful_jobs(self):
        for nav, code in ((None,3), ('bad NAV',3)):
            self.nav = nav
            with patch('gnss_logger.conversion.engine.subprocess.Popen',self.popen_fixture):
                job = ConversionJob(self.args(),self.source_list(), settings=replace(
                    load(self.config),allow_obs_without_nav=True)).start()
                report = job.result(wait=True,timeout=3)
            self.assertEqual(report['exit_code'],code)
            self.assertEqual(job.snapshot()['state'],'FAILED')
        self.returncode = 7
        with patch('gnss_logger.conversion.engine.subprocess.Popen',self.popen_fixture):
            job = ConversionJob(self.args(),self.source_list()).start()
            self.assertEqual(job.result(wait=True,timeout=3)['exit_code'],1)
        self.assertEqual(job.snapshot()['state'],'FAILED')

    def test_real_child_timeout_and_cancel_reap(self):
        original_popen = subprocess.Popen
        # Windows venv python.exe is a redirector, not the payload process.
        executable = str(getattr(sys, '_base_executable', None) or sys.executable)
        for cancelling in (False, True):
            cancel = Event()
            started = Event()
            children = []
            def spawn(*args, **kwargs):
                child = original_popen(*args, **kwargs)
                children.append(child)
                started.set()
                return child
            outcome = []
            ready = self.root / ('cancel-child.pid' if cancelling else 'timeout-child.pid')
            marker = ready.with_suffix('.ready')
            code = ("from pathlib import Path; import os,sys,time; "
                    "pid=Path(sys.argv[1]); pid.write_text(str(os.getpid())); "
                    "pid.with_suffix('.ready').touch(); time.sleep(60)")
            def run():
                try:
                    outcome.append(invoke([executable,'-c',code,str(ready)],
                                          self.root,.2 if not cancelling else 30,cancel))
                except KeyboardInterrupt:
                    outcome.append('cancelled')
            with patch('gnss_logger.conversion.engine.subprocess.Popen',spawn):
                thread = Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(started.wait(3))
                    if cancelling:
                        deadline = time.monotonic() + 3
                        pid = None
                        while time.monotonic() < deadline:
                            if marker.exists():
                                pid = int(ready.read_text())
                                break
                            time.sleep(.01)
                        self.assertEqual(pid, children[0].pid, 'Must cancel the actual payload, not a launcher')
                        cancel.set()
                    thread.join(4)
                    self.assertFalse(thread.is_alive())
                finally:
                    cancel.set()
                    thread.join(4)
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(outcome[0], 'cancelled') if cancelling else self.assertTrue(outcome[0]['timed_out'])

    def test_cancel_token_uses_popen_even_when_run_is_wrapped(self):
        with patch('gnss_logger.conversion.engine.subprocess.run',
                   side_effect=AssertionError('cancel path must not call run')), \
                patch('gnss_logger.conversion.engine.subprocess.Popen',self.popen_fixture):
            result = invoke([str(self.exe)],self.root,10,Event())
        self.assertEqual(result['returncode'],1)
        self.assertEqual(self.calls,[[str(self.exe)]])

    def test_worker_disable_admission_does_not_cancel_current(self):
        entered, release = Event(), Event()
        tokens = []
        def converting(args, sources, *, cancel):
            tokens.append(cancel)
            entered.set()
            release.wait(3)
            return dict(status='SUCCESS', errors=[], segments=[])
        artifact = dict(path=str(self.source),run_id='run',attempt_id='attempt',artifact_id='id',
                        bytes=len(self.original),sha256='unused',utc_day='2026-11-16')
        with patch('gnss_logger.conversion.worker.execute',converting):
            worker = SegmentWorker(self.config)
            try:
                self.assertTrue(worker.submit(artifact))
                self.assertTrue(entered.wait(2))
                worker.set_enabled(False)
                self.assertFalse(worker.submit(artifact))
                self.assertFalse(tokens[0].is_set())
                worker.cancel_current()
                self.assertTrue(tokens[0].is_set())
                worker.set_enabled(True)
                self.assertTrue(worker.submit(artifact))
            finally:
                release.set()
                result = worker.finish()
        self.assertEqual(result['statuses'], {'SUCCESS':2})
