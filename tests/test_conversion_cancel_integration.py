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
                code = ("from pathlib import Path; import sys,time; "
                        "Path('output.obs').write_text('incomplete'); "
                        "Path(sys.argv[1]).write_text('ready'); time.sleep(60)")
                return [sys.executable,'-c',code,str(ready)]
            code = ("from pathlib import Path; import sys; "
                    "Path('output.obs').write_text(sys.argv[1]); "
                    "Path('output.nav').write_text(sys.argv[2])")
            return [sys.executable,'-c',code,fixtures.obs_text(),fixtures.nav_text()]
        settings = replace(load(self.config),executable=Path(sys.executable),timeout_s=30)
        with patch('gnss_logger.conversion.engine.probe',probe), \
             patch('gnss_logger.conversion.engine.command',command), \
             patch('gnss_logger.conversion.engine.subprocess.Popen',spawn):
            job = ConversionJob(settings=settings,sources=[Source(self.source),Source(second)]).start()
            try:
                deadline = time.monotonic()+8
                while not ready.exists():
                    self.assertIsNone(job.result())
                    self.assertLess(time.monotonic(),deadline,'child did not reach ready barrier')
                    time.sleep(.01)
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
                self.assertFalse(list(root.rglob('.convbin-*')))
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
