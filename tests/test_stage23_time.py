from datetime import datetime, timedelta, timezone
import struct
import unittest

from gnss_logger.capture.framing import Framer
from gnss_logger.device.ubx import UBXParser, packet
from gnss_logger.mock import synthetic_epoch, synthetic_nav
from gnss_logger.recording.time_policy import TimeResolver

START = datetime(2026, 10, 28, 23, 59, 58, tzinfo=timezone.utc)


def raw(stamp=START, flags=1, leap=18):
    body = bytearray(synthetic_epoch(stamp)[6:-2]); body[10] = leap & 255; body[12] = flags
    return UBXParser().feed(packet(2, 0x15, body))[0]


class TimeTests(unittest.TestCase):
    def test_candidate_midnight_and_sfrbx(self):
        t = TimeResolver()
        nav = UBXParser().feed(synthetic_nav())[0]
        self.assertIsNone(t.update(nav, 0)['day'])
        self.assertTrue(t.update(raw(), 0)['hold'])
        result = t.update(raw(START+timedelta(seconds=1)), 10**9)
        self.assertEqual(result['release_day'], '2026-10-28')
        self.assertEqual(t.update(nav, 10**9)['day'], '2026-10-28')
        self.assertTrue(t.update(raw(START+timedelta(seconds=2)), 2*10**9)['hold'])
        self.assertEqual(t.update(raw(START+timedelta(seconds=3)), 3*10**9)['day'], '2026-10-29')

    def test_invalid_reset_stale_backward_and_leap(self):
        for flags, leap in ((0,18), (3,18), (1,19)):
            t=TimeResolver(); t.update(raw(),0); t.update(raw(START+timedelta(seconds=1)),10**9)
            self.assertIsNone(t.update(raw(START+timedelta(seconds=2), flags, leap),2*10**9)['day'])
            self.assertNotEqual(t.state,'VALID')
        t=TimeResolver(); t.update(raw(),0)
        self.assertTrue(t.update(raw(START-timedelta(seconds=1)),10**9)['release'])
        t=TimeResolver(); t.update(raw(),0); t.update(raw(START+timedelta(seconds=1)),10**9)
        self.assertIsNone(t.update((10,4,b''),7*10**9)['day'])
        self.assertEqual(t.generation,1)

    def test_dates_and_long_measurement_period(self):
        for stamp in (datetime(2028,2,29,23,59,59,tzinfo=timezone.utc),
                      datetime(2026,12,31,23,59,59,tzinfo=timezone.utc)):
            t=TimeResolver();t.update(raw(stamp),0)
            r=t.update(raw(stamp+timedelta(seconds=1)),10**9)
            self.assertEqual(r['day'], (stamp+timedelta(seconds=1)).date().isoformat())
        t=TimeResolver(1/30)
        t.update(raw(),0)
        self.assertEqual(t.ttl,90)
        self.assertEqual(t.update(raw(START+timedelta(seconds=30)),30*10**9)['day'],'2026-10-29')

    def test_framing_every_split_large_unknown_noise_and_tail(self):
        frames=[packet(0xff,0xfe,b'\xb5\x62'+b'x'*65533),synthetic_nav()]
        original=b'noise'+b''.join(frames)+b'\xb5\x62'
        f=Framer();tokens=[]
        for offset in range(0,len(original),37):tokens.extend(f.feed(original[offset:offset+37]))
        start,tail=f.tail()
        self.assertEqual(b''.join(r[1] for r in tokens)+tail,original)
        self.assertEqual([r[1] for r in tokens if r[2]],frames)
        self.assertEqual(start+len(tail),len(original))
        small=synthetic_nav()
        for split in range(len(small)+1):
            f=Framer();self.assertEqual([r[1] for r in list(f.feed(small[:split]))+list(f.feed(small[split:]))],[small])

    def test_bad_checksum_is_preserved_as_opaque(self):
        bad=bytearray(synthetic_nav());bad[-1]^=1
        f=Framer();tokens=list(f.feed(bytes(bad)+synthetic_nav()))
        self.assertEqual(b''.join(t[1] for t in tokens),bytes(bad)+synthetic_nav())
        self.assertEqual(sum(t[2] is not None for t in tokens),1)

    def test_gps_week_boundary_is_not_utc_midnight(self):
        # GPS week starts 18 s before this UTC Sunday midnight in this fixture.
        stamp=datetime(2026,10,31,23,59,41,tzinfo=timezone.utc)
        t=TimeResolver();t.update(raw(stamp),0)
        result=t.update(raw(stamp+timedelta(seconds=1)),10**9)
        self.assertEqual(result['day'],'2026-10-31')
        self.assertEqual(t.state,'VALID')

    def test_leap_change_both_directions_invalid_week_and_nan(self):
        for leap in (17,19):
            t=TimeResolver();t.update(raw(),0);t.update(raw(START+timedelta(seconds=1)),10**9)
            result=t.update(raw(START+timedelta(seconds=2),leap=leap),2*10**9)
            self.assertIsNone(result['day']);self.assertEqual(t.reason,'leap_ambiguity')
        for position,data in ((0,struct.pack('<d',float('nan'))),(8,struct.pack('<H',0))):
            body=bytearray(synthetic_epoch(START)[6:-2]);body[position:position+len(data)]=data
            t=TimeResolver();self.assertIsNone(t.update((2,0x15,bytes(body)),0)['day'])
            self.assertEqual(t.state,'DISCONTINUITY')
