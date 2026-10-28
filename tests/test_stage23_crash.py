"""Kill only a child created by this test, after its fsynced checkpoint barrier."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from gnss_logger.capture.names import reserve


CHILD = r'''
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json, sys, uuid
from gnss_logger.recording.projection import Projection
from gnss_logger.recording.storage import PrimaryJournal
from gnss_logger.recording.contracts import InputChunk
from gnss_logger.mock import synthetic_epoch, synthetic_nav
from gnss_logger.session import write_new_json
root=Path(sys.argv[1]); count=int(sys.argv[2]); internal=root/'internal';internal.mkdir()
owners=dict(zip(('run_id','attempt_id','connection_id','journal_id'),(uuid.uuid4().hex for _ in range(4))))
j=PrimaryJournal(internal/'primary.ubx.part')
p=Projection(internal,root/'data','UBX1',owners,j.path,1,lambda *a:None)
start=datetime(2026,10,28,23,59,58,tzinfo=timezone.utc)
for i in range(count):
    data=synthetic_epoch(start+timedelta(seconds=i))+synthetic_nav()
    c=InputChunk(owners['connection_id'],i,j.written,data,start.isoformat(),i*10**9)
    j.append(c);p.primary_end=j.written;p.receipt(c,len(data))
    j.sync();p.sync_receipts(j.synced);p.advance()
snapshot=p.checkpoint()
write_new_json(internal/'checkpoint-test.json',snapshot)
write_new_json(root/'ready.json',dict(synced=j.synced,sha256=j.digest.hexdigest()))
sys.stdin.read()  # Test parent kills this process; production does not use this barrier.
'''


class CrashTests(unittest.TestCase):
    def test_force_kill_before_and_after_rotation_preserves_durable_prefix_and_claims(self):
        import json
        for count in (2,4):
            with self.subTest(epochs=count), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                child=subprocess.Popen([sys.executable,'-B','-c',CHILD,str(root),str(count)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+10
                    while not (root/'ready.json').exists() and child.poll() is None and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue((root/'ready.json').exists(),'Child never reached durable checkpoint')
                    child.kill()
                    out,error=child.communicate(timeout=10)
                    self.assertNotEqual(child.returncode,0)
                    self.assertEqual(error,b'')
                finally:
                    if child.poll() is None:child.kill();child.communicate(timeout=10)
                ready=json.loads((root/'ready.json').read_text())
                primary=root/'internal/primary.ubx.part'
                self.assertEqual(primary.stat().st_size,ready['synced'])
                self.assertEqual(hashlib.sha256(primary.read_bytes()).hexdigest(),ready['sha256'])
                self.assertFalse((root/'internal/manifest.json').exists())
                old={p:p.read_bytes() for p in (root/'data').rglob('*') if p.is_file()}
                for day in ('2026-10-28','2026-10-29'):
                    handle,part,final=reserve(root/'data','UBX1',day);handle.close()
                    self.assertNotIn(part,old)
                for path,original in old.items():self.assertEqual(path.read_bytes(),original)
