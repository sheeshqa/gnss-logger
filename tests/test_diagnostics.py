import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from gnss_logger.cli import main, read_stream
from gnss_logger.device.transport import open_port
from gnss_logger.device.ubx import MON_VER_POLL, UBXParser, checksum, rawx
from gnss_logger.diagnostics import Diagnostics


def frame(cls, msg, payload):
    body = bytes((cls, msg)) + struct.pack("<H", len(payload)) + payload
    return b"\xb5\x62" + body + checksum(body)


def epoch(tow=100.0, week=2400, lock=1000, status=1, version=1, track=7):
    header = struct.pack("<dHbBBBBB", tow, week, 18, 1, status, version, 0, 0)
    measurement = struct.pack("<ddfBBBBHBBBBBB", 21000000.0, 110000000.5, -200.0,
                              0, 3, 0, 0, lock, 45, 1, 2, 3, track, 0)
    return frame(2, 0x15, header + measurement)


NAV = frame(2, 0x13, bytes((0, 3, 0, 0, 1, 0, 2, 0)) + bytes(4))


class FakePort:
    def __init__(self, chunks, fail=False):
        self.chunks = iter(chunks)
        self.writes = []
        self.fail = fail

    def read(self, count):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.fail:
                raise OSError("USB disconnected")
            return b""

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += 0.05
        return self.now


class ProtocolTests(unittest.TestCase):
    def test_all_chunk_boundaries_and_mixed_stream(self):
        stream = b"$GNGGA,noise\r\n" + epoch() + NAV
        for boundary in range(len(stream) + 1):
            d = Diagnostics()
            d.feed(stream[:boundary])
            d.feed(stream[boundary:])
            self.assertEqual((d.rawx_count, d.sfrbx_count, d.code_valid, d.phase_valid), (1, 1, 1, 1))
            self.assertEqual(d.parser.discarded_bytes, 14)
            self.assertEqual(d.report()["RAWX"]["signal_quality"]["0:0"]["satellites"], [3])

    def test_bytewise_checksum_recovery(self):
        corrupt = bytearray(epoch())
        corrupt[-1] ^= 1
        d = Diagnostics()
        for value in bytes(corrupt) + epoch() + NAV:
            d.feed(bytes((value,)))
        self.assertEqual(d.rawx_count, 1)
        self.assertEqual(d.parser.bad_checksums, 1)

    def test_oversize_and_truncation(self):
        p = UBXParser()
        p.feed(b"\xb5\x62\x01\x01\xff\xff" + NAV + epoch()[:10])
        self.assertEqual(p.oversize_headers, 1)
        self.assertEqual(len(p.buffer), 10)

    def test_raw_values_flags(self):
        payload = UBXParser().feed(epoch())[0][2]
        parsed = rawx(payload)
        m = parsed["measurements"][0]
        self.assertEqual(m["carrier_cycles"], 110000000.5)
        self.assertEqual(m["doppler_hz"], -200)
        self.assertTrue(parsed["leap_seconds_valid"])
        self.assertTrue(m["half_cycle_valid"])

    def test_unknown_version_and_bad_lengths_are_not_counted(self):
        d = Diagnostics()
        d.feed(epoch(version=3) + frame(2, 0x15, bytes(16)) + frame(2, 0x13, bytes(8)))
        self.assertEqual(d.rawx_count + d.sfrbx_count, 0)
        self.assertEqual(sum(d.decode_errors.values()), 3)

    def test_nonfinite_tow_rejected(self):
        d = Diagnostics()
        d.feed(epoch(float("nan")))
        self.assertEqual(d.rawx_count, 0)
        self.assertEqual(sum(d.decode_errors.values()), 1)

    def test_gap_lock_regression_clock_reset(self):
        d = Diagnostics()
        d.feed(epoch(100, lock=2000) + epoch(103, lock=100) + epoch(104, status=3))
        self.assertEqual(d.gap_events, 1)
        self.assertEqual(d.missing_estimate, 2)
        self.assertEqual(d.lock_regressions, 1)
        self.assertEqual(d.clock_resets, 1)

    def test_week_rollover_and_duplicates(self):
        d = Diagnostics()
        d.feed(epoch(604799) + epoch(0, week=2401) + epoch(0, week=2401))
        self.assertEqual(d.interval_min, 1)
        self.assertEqual(d.nonmonotonic_epochs, 1)

    def test_mon_ver_model_requires_mod(self):
        d = Diagnostics()
        payload = b"EXT CORE".ljust(30, b"\0") + b"HW".ljust(10, b"\0")
        d.feed(frame(10, 4, payload))
        self.assertIsNone(d.identity["model"])
        d.feed(frame(10, 4, payload + b"MOD=ZED-F9P".ljust(30, b"\0") +
                     b"PROTVER=27.50".ljust(30, b"\0")))
        self.assertEqual(d.identity["model"], "ZED-F9P")
        self.assertEqual(d.identity["protocol"], "27.50")


class SafetyTests(unittest.TestCase):
    def test_passive_no_writes_and_exact_capture(self):
        stream = b"NMEA noise" + epoch() + NAV + b"\xb5"
        port = FakePort([stream[:20], stream[20:]])
        capture = io.BytesIO()
        d = Diagnostics()
        read_stream(port, d, 1, capture, clock=Clock())
        self.assertEqual(port.writes, [])
        self.assertEqual(capture.getvalue(), stream)
        self.assertEqual(d.bytes_received, len(stream))

    def test_only_mon_ver_written_when_requested(self):
        port = FakePort([epoch(), NAV])
        read_stream(port, Diagnostics(), 1, request_version=True, clock=Clock())
        self.assertEqual(port.writes, [bytes.fromhex("b5620a0400000e34")])
        self.assertEqual(checksum(MON_VER_POLL[2:-2]), MON_VER_POLL[-2:])

    def test_silent_tail_is_detected(self):
        d = Diagnostics()
        read_stream(FakePort([epoch() + NAV]), d, 40, clock=Clock())
        self.assertTrue(d.liveness["rawx_stale"])
        self.assertTrue(d.liveness["sfrbx_stale"])

    def test_disconnect_propagates_with_partial_diagnostics(self):
        port = FakePort([epoch()], fail=True)
        d = Diagnostics()
        with self.assertRaisesRegex(OSError, "USB disconnected"):
            read_stream(port, d, 10, clock=Clock())
        self.assertEqual(d.rawx_count, 1)

    def test_host_lines_set_before_open(self):
        events = []
        class Serial:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
            def __setattr__(self, name, value):
                events.append((name, value))
                self.__dict__[name] = value
            def open(self):
                events.append(("open", None))
            def close(self):
                pass
        import types
        with patch.dict("sys.modules", serial=types.SimpleNamespace(Serial=Serial)):
            open_port("COM9", 115200)
        self.assertLess(events.index(("dtr", False)), events.index(("open", None)))
        self.assertLess(events.index(("rts", False)), events.index(("open", None)))

    def test_disk_failure_propagates(self):
        class Disk:
            def write(self, data):
                raise OSError("disk full")
        with self.assertRaisesRegex(OSError, "disk full"):
            read_stream(FakePort([epoch()]), Diagnostics(), 1, Disk(), clock=Clock())

    def test_cli_replay_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "test.ubx"
            report = Path(tmp) / "report.json"
            source.write_bytes(epoch() + epoch(101) + NAV)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["replay", str(source), "--json", str(report)]), 0)
                content = report.read_bytes()
                self.assertEqual(main(["replay", str(source), "--json", str(report)]), 1)
            self.assertEqual(report.read_bytes(), content)
            self.assertEqual(json.loads(content)["RAWX"]["epochs"], 2)

    def test_cli_disconnect_writes_report_and_closes(self):
        port = FakePort([epoch()], fail=True)
        with tempfile.TemporaryDirectory() as tmp, patch("gnss_logger.cli.open_port", return_value=port):
            report = Path(tmp) / "disconnect.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["diagnose", "--port", "COM9", "--json", str(report)])
            self.assertEqual(code, 1)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertIn("USB disconnected", data["error"])
            self.assertEqual(data["RAWX"]["epochs"], 1)
            self.assertTrue(port.closed)

    def test_cli_absence_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "nmea.bin"
            source.write_bytes(b"$GNGGA,anything\r\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["replay", str(source)]), 2)


if __name__ == "__main__":
    unittest.main()
