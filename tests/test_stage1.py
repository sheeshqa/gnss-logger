import ast
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from gnss_logger.capture.writer import UTCWriter
from gnss_logger.cli import main
from gnss_logger.device.adapters import detect_adapter, F9PAdapter, X20PAdapter
from gnss_logger.device.requests import ensure_read_only, Probe
from gnss_logger.device.transport import send_read_only
from gnss_logger.device.ubx import MON_VER_POLL, UBXParser, packet, valget, valget_request, value_size
from gnss_logger.diagnostics import Diagnostics
from gnss_logger.engine import run_source
from gnss_logger.mock import MockReceiver, synthetic_epoch, synthetic_nav
from gnss_logger.sources import ReplaySource, file_records
from gnss_logger.timebase import parse_utc


UTC = timezone.utc
START = datetime(2026, 3, 5, 23, 59, 59, tzinfo=UTC)


class VirtualClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run_mock(profile="f9p", scenario="normal", epochs=3):
    source = MockReceiver(profile, epochs=epochs, scenario=scenario)
    d = Diagnostics()
    probe = Probe(source.send, inspect=True)
    run_source(source, d, probe=probe, clock=source.monotonic)
    return source, d, probe


class RequestTests(unittest.TestCase):
    def test_known_packet_and_bad_arguments(self):
        self.assertEqual(packet(10, 4), MON_VER_POLL)
        for cls, msg in ((-1, 0), (256, 0), (0, 300)):
            with self.assertRaises(ValueError):
                packet(cls, msg)
        with self.assertRaises(ValueError):
            packet(0, 0, bytes(65536))

    def test_valget_request_independent_expected_bytes(self):
        p = valget_request([0x30210001])
        self.assertEqual(p[6:-2], bytes.fromhex("0000000001002130"))
        self.assertEqual(valget(p[6:-2])["keys"], [0x30210001])

    def test_valget_response_all_storage_widths(self):
        values = {0x10010001: 1, 0x20010001: 200, 0x30010001: 1000,
                  0x40010001: 100000, 0x50010001: 100000000000}
        body = b"\x01\0\0\0" + b"".join(struct.pack("<I", k) + v.to_bytes(value_size(k), "little")
                                             for k, v in values.items())
        self.assertEqual(valget(body)["values"], {f"{k:08X}": v for k, v in values.items()})

    def test_bad_valget_rejected(self):
        for body in (b"", b"\x02\0\0\0", b"\0\0\0\0\x01", b"\x01\0\0\0\x01",
                     b"\x01\0\0\0\x01\0\x21\x30\x01", b"\x01\0\0\0\x01\0\0\x70"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                valget(body)

    def test_guard_rejects_every_mutation_and_arbitrary_read(self):
        for data in (packet(6, 0x8A, bytes(4)), packet(6, 0x8C, bytes(4)), packet(6, 4, bytes(4)),
                     packet(6, 1, bytes(3)), packet(10, 4, b"x"), MON_VER_POLL + MON_VER_POLL,
                     valget_request([0x30210001], layer=1), valget_request([0x0FFF0000]),
                     b"garbage" + MON_VER_POLL):
            with self.subTest(data=data), self.assertRaises(PermissionError):
                ensure_read_only(data)
        ensure_read_only(MON_VER_POLL)
        ensure_read_only(valget_request([0x30210001, 0x209102A7, 0x20910234]))

    def test_guard_runs_before_physical_write(self):
        class Port:
            def write(self, data):
                raise AssertionError("Physical write must not happen")
        with self.assertRaises(PermissionError):
            send_read_only(Port(), packet(6, 0x8A, bytes(4)))

    def test_short_read_request_write_is_error(self):
        class Port:
            def write(self, data):
                return 0
        with self.assertRaises(OSError):
            send_read_only(Port(), MON_VER_POLL)

    def test_both_profiles_detect_and_read_own_subset(self):
        for profile, adapter_class in (("f9p", F9PAdapter), ("x20p", X20PAdapter)):
            with self.subTest(profile=profile):
                source, d, probe = run_mock(profile)
                self.assertIsInstance(d.adapter, adapter_class)
                self.assertEqual(d.identity["firmware"], "MOCK-" + profile.upper())
                self.assertEqual(d.configuration["0"]["30210001"], 1000)
                self.assertEqual(len(probe.results), 2)
                self.assertIsNone(probe.pending)
                self.assertEqual(d.rawx_count, 3)
                self.assertEqual(len(source.writes), 2)
                with self.assertRaises(PermissionError):
                    d.adapter.apply_tec_profile({})
        self.assertIsNone(detect_adapter({"model": "some-other-model"}))
        self.assertIsNot(F9PAdapter.keys, X20PAdapter.keys)

    def test_partial_responses(self):
        _, d, probe = run_mock(scenario="partial")
        self.assertEqual(d.config_pages, 1)
        self.assertEqual(d.parser.bad_checksums, 0)
        self.assertIsNone(probe.pending)

    def test_timeout_and_corrupt_malformed_responses(self):
        for scenario in ("timeout", "corrupt", "malformed"):
            with self.subTest(scenario=scenario):
                _, d, probe = run_mock(scenario=scenario)
                self.assertEqual(probe.results[-1]["status"], "timeout")
                self.assertIsNone(probe.pending)
                self.assertIsNone(d.identity)
                if scenario == "corrupt":
                    self.assertGreater(d.parser.bad_checksums, 0)
                if scenario == "malformed":
                    self.assertGreater(sum(d.decode_errors.values()), 0)

    def test_ack_then_data_and_nak(self):
        _, _, probe = run_mock(scenario="ack")
        self.assertEqual([r["status"] for r in probe.results], ["response", "ack", "response"])
        _, d, probe = run_mock(scenario="nak")
        self.assertEqual(probe.results[-1]["status"], "nak")
        self.assertIsNone(probe.pending)
        self.assertEqual(d.ack_counts["NAK:06-8B"], 1)

    def test_ack_without_response_times_out(self):
        _, _, probe = run_mock(scenario="ack-only")
        self.assertEqual([r["status"] for r in probe.results], ["response", "ack", "timeout"])

    def test_unrelated_ack_and_logged_poll_do_not_complete_request(self):
        sent = []
        p = Probe(sent.append)
        p.start(0)
        p.handle((10, 4, b""), 0.5)
        p.handle((5, 0, b"\x06\x8b"), 0.5)
        self.assertIsNotNone(p.pending)
        p.tick(2)
        self.assertEqual(p.results[-1]["status"], "timeout")

    def test_missing_config_keys_do_not_complete_request(self):
        _, d, _ = run_mock()
        p = Probe(lambda b: None, inspect=True)
        p.start(0)
        # Recreate a valid mock identification response.
        m = MockReceiver()
        m.send(MON_VER_POLL)
        p.handle(UBXParser().feed(m.responses.popleft())[0], 0)
        p.handle((6, 0x8B, bytes((1, 0, 0, 0))), 1)
        p.tick(2)
        self.assertEqual(p.results[-1]["status"], "timeout")

    def test_logged_requests_are_not_decode_errors(self):
        d = Diagnostics()
        d.feed(MON_VER_POLL + valget_request([0x30210001]))
        self.assertFalse(d.decode_errors)
        self.assertEqual(sum(d.logged_requests.values()), 2)


class ReplayRecordingTests(unittest.TestCase):
    def test_memory_capture_over_virtual_flush_interval(self):
        source = MockReceiver(epochs=8)
        capture = io.BytesIO()
        d = Diagnostics()
        run_source(source, d, capture=capture, clock=source.monotonic)
        self.assertEqual(d.rawx_count, 8)
        self.assertEqual(len(capture.getvalue()), d.bytes_received)

    def test_framer_records_are_lossless_with_corruption(self):
        stream = b"noise\xb5" + MON_VER_POLL + b"\xb5\x62\x01\x01\xff\xff" + synthetic_nav() + b"\xb5\x62\x02"
        p = UBXParser()
        records = []
        for i in range(0, len(stream), 3):
            records.extend(p.feed_records(stream[i:i + 3]))
        self.assertEqual(b"".join(b for b, _ in records) + bytes(p.buffer), stream)

    def test_replay_all_speeds_keep_bytes_and_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.ubx"
            stream = b"noise" + synthetic_epoch(START) + synthetic_nav() + synthetic_epoch(START + timedelta(seconds=2))
            path.write_bytes(stream)
            for speed in (0, 1, 10):
                clock = VirtualClock()
                source = ReplaySource(path, speed, clock=clock, sleep=clock.sleep)
                capture = io.BytesIO()
                d = Diagnostics(expected_hz=0.5)
                digest = run_source(source, d, capture=capture)
                self.assertEqual(capture.getvalue(), stream)
                self.assertEqual(digest, hashlib.sha256(stream).hexdigest())
                self.assertAlmostEqual(clock.now, 0 if speed == 0 else 2 / speed)
                self.assertEqual(d.interval_max, 2)
                self.assertEqual(d.gap_events, 0)

    def test_utc_rotation_preserves_frames_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.ubx"
            before = b"noise" + synthetic_epoch(START) + synthetic_nav()
            after = synthetic_epoch(START + timedelta(seconds=1)) + synthetic_nav() + b"\xb5"
            path.write_bytes(before + after)
            writer = UTCWriter(Path(tmp) / "segments")
            run_source(ReplaySource(path), Diagnostics(), writer=writer)
            self.assertIsNone(writer.file)
            self.assertEqual(len(writer.segments), 2)
            self.assertEqual([s["utc_day"] for s in writer.segments], ["2026-03-05", "2026-03-06"])
            self.assertEqual([Path(s["path"]).read_bytes() for s in writer.segments], [before, after])
            for segment in writer.segments:
                self.assertEqual(segment["status"], "closed")
                self.assertEqual(segment["sha256"], hashlib.sha256(Path(segment["path"]).read_bytes()).hexdigest())

    def test_restarts_and_clock_backwards_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for _ in range(2):
                with UTCWriter(tmp) as writer:
                    writer.write(b"a", START)
                    writer.write(b"b", START + timedelta(days=1))
                    writer.write(b"c", START)
                paths.extend(s["path"] for s in writer.segments)
            self.assertEqual(len(set(paths)), 6)
            self.assertEqual(b"".join(Path(p).read_bytes() for p in paths), b"abcabc")

    def test_empty_recording_creates_no_binary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with UTCWriter(tmp) as writer:
                writer.write(b"", START)
            self.assertFalse(writer.segments)
            self.assertFalse(list(Path(tmp).glob("*.ubx")))

    def test_unknown_utc_requires_explicit_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notimed.ubx"
            path.write_bytes(MON_VER_POLL)
            with UTCWriter(Path(tmp) / "segments") as writer:
                with self.assertRaisesRegex(ValueError, "known"):
                    run_source(ReplaySource(path), Diagnostics(), writer=writer)
            with UTCWriter(Path(tmp) / "anchored") as writer:
                run_source(ReplaySource(path, start_utc=START), Diagnostics(), writer=writer)
            self.assertEqual(Path(writer.segments[0]["path"]).read_bytes(), MON_VER_POLL)

    def test_interrupt_closes_partial_segment(self):
        class Source:
            finite = True
            def __iter__(self):
                from gnss_logger.sources import Chunk
                yield Chunk(synthetic_nav(), START)
                raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as tmp:
            writer = UTCWriter(tmp)
            with self.assertRaises(KeyboardInterrupt):
                run_source(Source(), Diagnostics(), writer=writer)
            self.assertIsNone(writer.file)
            self.assertEqual(writer.segments[0]["status"], "closed_partial")
            with Path(writer.segments[0]["path"]).open("ab") as handle:
                handle.write(b"x")  # Handle is released.

    def test_writer_io_failure_is_failed_not_closed_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = UTCWriter(tmp)
            writer.write(b"a", START)
            with patch.object(writer.file, "write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    writer.write(b"b", START)
            writer.close()
            self.assertEqual(writer.segments[0]["status"], "failed")

    def test_gap_times_and_no_interpolation_30_seconds(self):
        d = Diagnostics()
        d.feed(synthetic_epoch(START) + synthetic_epoch(START + timedelta(seconds=30)))
        report = d.report()
        self.assertEqual(d.rawx_count, 2)
        self.assertEqual(d.missing_estimate, 29)
        self.assertEqual(report["timing"]["gaps"][0]["interval_s"], 30)
        self.assertEqual(report["timing"]["gaps"][0]["start_receiver_derived_utc"], START.isoformat())
        d = Diagnostics(1 / 30)
        d.feed(synthetic_epoch(START) + synthetic_epoch(START + timedelta(seconds=30)))
        self.assertEqual(d.gap_events, 0)

    def test_mock_capture_stays_labelled_in_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, report = Path(tmp) / "MOCK.ubx", Path(tmp) / "mock.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["mock", "--capture", str(path), "--json", str(report)]), 0)
            self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["simulated"])
            replay = ReplaySource(path)
            self.assertTrue(replay.simulated)
            self.assertTrue(Path(str(path) + ".meta.json").exists())

    def test_cli_absent_port_is_controlled_error(self):
        with patch("gnss_logger.cli.open_port", side_effect=OSError("port not found")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["serial", "--port", "COM_NOT_PRESENT"]), 1)
        report = json.loads(output.getvalue())
        self.assertIn("replay", report["error"])
        self.assertEqual(report["hardware_test_status"], "NOT TESTED")

    def test_no_ports_list_is_informative(self):
        with patch("serial.tools.list_ports.comports", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["ports"]), 0)
        self.assertIn("replay", output.getvalue())

    def test_cli_all_mock_scenarios_return_explicit_status(self):
        for scenario, expected in (("normal", 0), ("partial", 0), ("ack", 0), ("no-data", 2),
                                   ("timeout", 3), ("nak", 3), ("ack-only", 3), ("corrupt", 3), ("malformed", 3)):
            with self.subTest(scenario=scenario), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["mock", "--scenario", scenario]), expected)
                self.assertTrue(json.loads(output.getvalue())["simulated"])

    def test_python39_syntax_and_timezone(self):
        for path in Path("gnss_logger").rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
        self.assertEqual(parse_utc("2026-03-06T08:00:00+08:00").hour, 0)
        with self.assertRaises(ValueError):
            parse_utc("2026-03-06T00:00:00")
