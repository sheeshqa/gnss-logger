import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gnss_logger.capture.names import reserve, station_component
from gnss_logger.recording.schema import get_schema, validate_record


class NamesTests(unittest.TestCase):
    def test_restarts_and_stale_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            a, part, final = reserve(folder, 'UBX1', '2026-10-28')
            a.write(b'OLD'); a.close()
            b, second, _ = reserve(folder, 'UBX1', '2026-10-28'); b.close()
            self.assertEqual(second.name, 'UBX1_20261028_001.ubx.part')
            part.rename(final)
            c, third, _ = reserve(folder, 'UBX1', '2026-10-28'); c.close()
            self.assertEqual(third.name, 'UBX1_20261028_002.ubx.part')
            self.assertEqual(final.read_bytes(), b'OLD')
            d, unknown, _ = reserve(folder, 'UBX1', None); d.close()
            self.assertEqual(unknown.parent.name, 'undated')

    def test_two_processes_claim_different_names(self):
        with tempfile.TemporaryDirectory() as folder:
            script = ('from gnss_logger.capture.names import reserve; import sys; '
                      'h,p,f=reserve(sys.argv[1],"UBX1","2026-10-28"); '
                      'h.write(b"original"); h.close(); print(p.name)')
            children = [subprocess.Popen([sys.executable, '-B', '-c', script, folder],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            names = []
            for child in children:
                out, error = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, error)
                names.append(out.strip())
            self.assertEqual(len(set(names)), 2)

    def test_unsafe_station_and_bad_day(self):
        for value in ('../UBX1', 'CON', 'COM1', 'a/b', '', 'A.'):
            with self.assertRaises(ValueError): station_component(value)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError): reserve(folder, 'UBX1', '2026-02-30')

    def test_v1_unchanged_and_v2_explicit(self):
        root = Path(__file__).resolve().parents[1] / 'docs/schemas/stage2'
        for kind in ('manifest', 'checkpoint', 'event', 'diagnostic'):
            self.assertEqual(get_schema(kind), json.loads((root/(kind+'-v1.json')).read_text()))
            v2 = get_schema(kind, 2)
            self.assertEqual(v2,json.loads((root/(kind+'-v2.json')).read_text()))
            self.assertEqual(v2['properties']['schema_version']['const'], 2)
            self.assertEqual(v2['properties']['contract_version']['const'], 'stage2.3/v2')
        with self.assertRaises(ValueError): get_schema('event', 3)
        for kind in ('artifact','receipt','range'):
            self.assertEqual(get_schema(kind,2),json.loads((root/(kind+'-v2.json')).read_text()))
