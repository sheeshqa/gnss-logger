"""A real long-lived child inside the complete conversion job; no receiver."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import time
from unittest import TestCase
from unittest.mock import patch

import test_stage31_conversion as fixtures
from gnss_logger.conversion import ConversionJob
from gnss_logger.conversion.config import load
from gnss_logger.conversion.selection import Source
from gnss_logger.session import file_fingerprint


class JobProcessIntegrationTests(TestCase):
    setUp = fixtures.ConversionTests.setUp

    def test_cancel_real_child_cleans_work_and_keeps_committed_segment(self):
        second = self.root/'second.ubx'
        second.write_bytes(self.original)
        ready = self.root/'child-ready'
        published = ready.with_suffix('.published')
        # Windows venv python.exe is a redirector: Popen owns the launcher,
        # not the interpreter holding cwd. Model a direct native convbin child.
        executable = Path(getattr(sys, '_base_executable', None) or sys.executable)
        children = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process
        def probe(settings, engine, cancel=None):
            engine.update(path=str(settings.executable),options=[],**file_fingerprint(settings.executable))
        def command(settings, source, options):
            if source.path == second:
                code = ("from pathlib import Path; import os,sys,time; "
                        "Path('output.obs').write_text('incomplete'); "
                        "ready=Path(sys.argv[1]); ready.write_text(str(os.getpid())); "
                        "ready.with_suffix('.published').mkdir(); time.sleep(60)")
                return [str(executable),'-c',code,str(ready)]
            code = ("from pathlib import Path; import sys; "
                    "Path('output.obs').write_text(sys.argv[1]); "
                    "Path('output.nav').write_text(sys.argv[2])")
            return [str(executable),'-c',code,fixtures.obs_text(),fixtures.nav_text()]
        settings = replace(load(self.config),executable=executable,timeout_s=30)
        with patch('gnss_logger.conversion.engine.probe',probe), \
             patch('gnss_logger.conversion.engine.command',command), \
             patch('gnss_logger.conversion.engine.subprocess.Popen',spawn):
            job = ConversionJob(settings=settings,sources=[Source(self.source),Source(second)]).start()
            try:
                deadline = time.monotonic()+8
                actual_pid = None
                while actual_pid is None or len(children) != 2:
                    self.assertIsNone(job.result())
                    self.assertLess(time.monotonic(),deadline,'child did not reach ready barrier')
                    # The marker is created after write_text has closed the PID
                    # file. Reading during a Windows rename can deny sharing.
                    if published.is_dir():
                        actual_pid = int(ready.read_text())
                    if actual_pid is None or len(children) != 2:
                        time.sleep(.01)
                self.assertGreater(actual_pid, 0)
                self.assertEqual(actual_pid, children[1].pid,
                                 'fixture interpreter must be the direct Popen child')
                progress = job.snapshot()['progress']
                first = progress['segments'][0]
                self.assertIn(first['status'],('SUCCESS','SUCCESS_WITH_WARNINGS'))
                saved = {path:Path(path).read_bytes() for path in first['published_outputs'].values()}
                job.cancel()
                report = job.result(wait=True,timeout=4)
                self.assertIsNotNone(report,'job did not finish cancellation')
                self.assertEqual((report['status'],report['exit_code']),('INTERRUPTED',130))
                self.assertEqual(job.snapshot()['state'],'CANCELLED')
                self.assertEqual(report['segments'][1]['published_outputs'],{})
                self.assertEqual(saved,{path:Path(path).read_bytes() for path in saved})
                root = Path(report['output_directory'])
                remaining = list(root.rglob('.convbin-*'))
                details = {}
                for path in remaining:
                    detail = dict(exists=path.exists(), is_dir=path.is_dir(),
                                  contents=[str(item) for item in path.rglob('*')])
                    try:
                        detail['stat'] = repr(path.stat())
                    except OSError as error:
                        detail['stat_error'] = repr(error)
                    details[str(path)] = detail
                self.assertFalse(remaining, dict(report=report, remaining=details,
                    children=[dict(pid=child.pid, returncode=child.poll()) for child in children]))
                self.assertFalse((root/'second.obs').exists())
                self.assertFalse((root/'second.conversion.json').exists())
                self.assertEqual(self.source.read_bytes(),self.original)
                self.assertEqual(second.read_bytes(),self.original)
                self.assertEqual(len(children),2)
                self.assertTrue(all(child.poll() is not None for child in children))
            finally:
                job.cancel()
                job.result(wait=True,timeout=4)
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
