import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gnss_logger import __version__
from gnss_logger.cli import main
from gnss_logger.device.ports import port_info
from gnss_logger.diagnostics import Diagnostics
from gnss_logger.engine import RunState, run_source
from gnss_logger.mock import synthetic_epoch, synthetic_nav
from gnss_logger.phase import PhaseTracker
from gnss_logger.session import CaptureFile
from gnss_logger.sources import Chunk, SerialSource
from gnss_logger.device.ubx import packet, rawx

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observation(second=0, lock=1000, track=7, sv=3, sig=0, status=1):
    body = bytearray(synthetic_epoch(START + timedelta(seconds=second), (sig,), lock)[6:-2])
    body[12] = status
    body[37] = sv
    body[46] = track
    return packet(2, 0x15, body)


class TimedSource:
    finite = False

    def __init__(self, steps):
        self.now = 0.0
        self.steps = steps
        self.reads = 0

    def __iter__(self):
        for instant, data in self.steps:
            self.now = instant
            self.reads += 1
            yield Chunk(data, START + timedelta(seconds=instant))

    def clock(self):
        return self.now


class DeadlineRegressionTests(unittest.TestCase):
    def run_timed(self, steps, duration=1):
        source = TimedSource(steps)
        d, state, capture = Diagnostics(), RunState(), io.BytesIO()
        run_source(source, d, duration, capture=capture, state=state, clock=source.clock)
        return source, d, state, capture.getvalue()

    def test_read_before_deadline_and_exhaustion(self):
        source, d, state, data = self.run_timed([(0.2, b"A"), (0.7, b"B")])
        self.assertEqual(data, b"AB")
        self.assertEqual(state.stop_reason, "source_exhausted")
        self.assertEqual(state.bytes_received, d.bytes_received)

    def test_crossing_deadline_never_drops_returned_bytes_or_starts_next_read(self):
        source, d, state, data = self.run_timed([(0.25, b"FIRST"), (1.01, b"TAIL"), (2, b"NOT_READ")])
        self.assertEqual(data, b"FIRSTTAIL")
        self.assertEqual(source.reads, 2)
        self.assertEqual(state.stop_reason, "deadline")
        self.assertEqual(state.bytes_received, 9)
        self.assertEqual(d.bytes_received, 9)
        self.assertAlmostEqual(state.deadline_overrun_s, .01)
        self.assertEqual(state.digest.hexdigest(), hashlib.sha256(data).hexdigest())

    def test_empty_read_crossing_deadline_is_counted(self):
        source, d, state, data = self.run_timed([(0.2, b""), (1.01, b""), (2, b"x")])
        self.assertEqual(source.reads, 2)
        self.assertEqual(state.empty_reads, 2)
        self.assertEqual(data, b"")
        self.assertEqual(state.stop_reason, "deadline")
        self.assertIsNotNone(state.last_successful_read_utc)
        self.assertIsNone(state.last_data_read_utc)

    def test_partial_packet_completed_by_crossing_read(self):
        stream = observation()
        source, d, state, data = self.run_timed([(0.2, stream[:19]), (1.01, stream[19:])])
        self.assertEqual(data, stream)
        self.assertEqual(d.rawx_count, 1)
        self.assertEqual(len(d.parser.buffer), 0)

    def test_incomplete_packet_is_preserved_and_diagnosed(self):
        stream = observation()[:19]
        _, d, state, data = self.run_timed([(1.01, stream)])
        self.assertEqual(data, stream)
        self.assertEqual(len(d.parser.buffer), 19)
        self.assertEqual(d.rawx_count, 0)

    def test_exact_deadline_chunk_is_processed(self):
        source, _, state, data = self.run_timed([(1.0, b"at_boundary"), (2, b"x")])
        self.assertEqual(data, b"at_boundary")
        self.assertEqual(source.reads, 1)
        self.assertEqual(state.stop_reason, "deadline")

    def test_serial_timeout_is_bounded_and_restored(self):
        class Port:
            timeout = .25
            now = 0
            timeouts = []
            def read(self, count):
                self.timeouts.append(self.timeout)
                self.now += self.timeout
                return b"x"
        port = Port()
        state = RunState()
        run_source(SerialSource(port), Diagnostics(), .6, state=state, clock=lambda: port.now)
        self.assertEqual(len(port.timeouts), 3)
        self.assertAlmostEqual(port.timeouts[-1], .1)
        self.assertEqual(port.timeout, .25)
        self.assertEqual(state.bytes_received, 3)
        self.assertTrue(state.timeout_bounded)

    def test_timeout_restored_on_com_error(self):
        class Port:
            timeout = .25
            def read(self, count):
                raise OSError("unplugged")
        port = Port()
        state = RunState()
        with self.assertRaises(OSError):
            run_source(SerialSource(port), Diagnostics(), .1, state=state, clock=lambda: 0)
        self.assertEqual(port.timeout, .25)
        self.assertEqual(state.stop_reason, "source_error")

    def test_finalization_failure_is_not_a_successful_return(self):
        source = TimedSource([(1.01, b"saved")])
        state = RunState()
        with patch("gnss_logger.engine.sync_sink", side_effect=OSError("fsync failed")):
            with self.assertRaisesRegex(OSError, "finalization"):
                run_source(source, Diagnostics(), 1, capture=io.BytesIO(), state=state, clock=source.clock)
        self.assertEqual(state.stop_reason, "finalization_error")
        self.assertEqual(state.bytes_received, 5)
        self.assertTrue(state.finalization_errors)


class FakePort:
    timeout = .25
    def __init__(self, data, error):
        self.data, self.error = data, error
        self.closed = False
        self.writes = []

    def read(self, count):
        if self.data is not None:
            data, self.data = self.data, None
            return data
        raise self.error

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class SessionRegressionTests(unittest.TestCase):
    def cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, json.loads(stdout.getvalue())

    def test_error_and_interrupt_finalize_partial_capture_manifest_and_journal(self):
        import serial
        stream = observation() + synthetic_nav()
        for error, expected_code, expected_reason in (
            (serial.SerialException("USB disconnected"), 1, "source_error"),
            (KeyboardInterrupt(), 130, "keyboard_interrupt")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                binary = Path(tmp) / "partial.ubx"
                port = FakePort(stream, error)
                with patch("gnss_logger.cli.open_port", return_value=port):
                    code, report = self.cli(["serial", "--port", "COM99", "--capture", str(binary),
                                             "--session-dir", str(Path(tmp) / "sessions")])
                self.assertEqual(code, expected_code)
                self.assertEqual(report["stop_reason"], expected_reason)
                self.assertEqual(report["registration"]["status"], "PARTIAL")
                self.assertEqual(report["stream_sha256"], hashlib.sha256(stream).hexdigest())
                self.assertEqual(report["bytes_received"], len(stream))
                self.assertIsNotNone(report["run"]["last_data_read_utc"])
                self.assertIsNotNone(report["run"]["last_successful_read_utc"])
                artifact = report["artifacts"][0]
                self.assertEqual(artifact["status"], "closed_partial")
                self.assertTrue(artifact["matches_received_stream"])
                self.assertFalse(artifact["complete"])
                manifest = json.loads(Path(report["paths"]["manifest"]).read_text(encoding="utf-8"))
                self.assertEqual(manifest["session_id"], report["session_id"])
                self.assertEqual(manifest["stop_reason"], expected_reason)
                self.assertFalse(manifest["artifacts"][0]["complete"])
                self.assertEqual(manifest["artifacts"][0]["sha256"], report["stream_sha256"])
                meta = json.loads(Path(str(binary) + ".meta.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["session_id"], report["session_id"])
                self.assertFalse(meta["complete"])
                events = [json.loads(line) for line in Path(report["paths"]["events"]).read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(e["event"] == "error" for e in events))
                self.assertTrue(port.closed)
                self.assertFalse(port.writes)

    def test_normal_recording_complete_and_phase_quality_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, capture = Path(tmp) / "input.ubx", Path(tmp) / "copy.ubx"
            stream = observation(0) + observation(1, track=1) + synthetic_nav()
            source.write_bytes(stream)
            code, report = self.cli(["replay", str(source), "--capture", str(capture),
                                    "--session-dir", str(Path(tmp) / "sessions")])
            self.assertEqual(code, 0)
            self.assertEqual(report["registration"]["status"], "SUCCESS")
            self.assertEqual(report["phase_quality"]["status"], "WARNINGS")
            self.assertEqual(report["phase_quality"]["counts"]["phase_validity_lost"], 1)
            self.assertTrue(report["artifacts"][0]["complete"])
            self.assertEqual(capture.read_bytes(), stream)
            self.assertEqual(report["hardware_test_status"], "NOT TESTED")

    def test_every_attempt_gets_unique_id_even_on_output_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path, capture = Path(tmp) / "report.json", Path(tmp) / "copy.ubx"
            args = ["mock", "--json", str(report_path), "--capture", str(capture)]
            code1, first = self.cli(args)
            before_report, before_capture = report_path.read_bytes(), capture.read_bytes()
            with patch("gnss_logger.cli.open_port") as open_port:
                code2, second = self.cli(args)
            self.assertEqual((code1, code2), (0, 1))
            self.assertNotEqual(first["session_id"], second["session_id"])
            self.assertEqual(report_path.read_bytes(), before_report)
            self.assertEqual(capture.read_bytes(), before_capture)
            self.assertIn("FileExistsError", second["error"])
            self.assertTrue(Path(second["paths"]["report"]).exists())
            self.assertTrue(Path(second["paths"]["manifest"]).exists())
            open_port.assert_not_called()

    def test_versions_parameters_paths_and_requested_duration_saved(self):
        with tempfile.TemporaryDirectory() as tmp, patch("gnss_logger.cli.open_port", side_effect=OSError("absent")):
            code, report = self.cli(["serial", "--port", "COM99", "--seconds", "600",
                                     "--session-dir", tmp])
            self.assertEqual(code, 1)
            self.assertEqual(report["parameters"]["seconds"], 600)
            self.assertEqual(report["parameters"]["port"], "COM99")
            self.assertEqual(report["connection"]["baudrate"], 115200)
            self.assertEqual(report["versions"]["gnss_logger"], __version__)
            self.assertTrue(report["versions"]["python"])
            self.assertEqual(report["versions"]["dependencies"]["pyserial"], "3.5")
            self.assertIn("--seconds", report["argv"])
            self.assertIn("source_open", report["errors"][0]["component"])
            self.assertTrue(report["started_utc"])
            self.assertTrue(report["ended_utc"])

    def test_empty_capture_for_absent_port_is_partial_and_hashed(self):
        with tempfile.TemporaryDirectory() as tmp, patch("gnss_logger.cli.open_port", side_effect=OSError("absent")):
            binary = Path(tmp) / "empty.ubx"
            code, report = self.cli(["serial", "--port", "COM99", "--capture", str(binary), "--session-dir", tmp])
            self.assertEqual(code, 1)
            self.assertEqual(report["artifacts"][0]["bytes"], 0)
            self.assertEqual(report["artifacts"][0]["sha256"], hashlib.sha256(b"").hexdigest())
            self.assertFalse(report["artifacts"][0]["complete"])
            self.assertTrue(Path(str(binary) + ".meta.json").exists())

    def test_short_write_is_explicit_and_actual_file_is_hashed(self):
        original_write = CaptureFile.write
        def short_write(sink, data):
            return original_write(sink, data[:2])
        with tempfile.TemporaryDirectory() as tmp, patch("gnss_logger.cli.CaptureFile.write", short_write):
            binary = Path(tmp) / "short.ubx"
            code, report = self.cli(["mock", "--capture", str(binary), "--session-dir", tmp])
            self.assertEqual(code, 1)
            artifact = report["artifacts"][0]
            self.assertEqual(artifact["bytes"], 2)
            self.assertGreater(artifact["unarchived_bytes"], 0)
            self.assertFalse(artifact["matches_received_stream"])
            self.assertFalse(artifact["complete"])
            self.assertEqual(artifact["sha256"], hashlib.sha256(binary.read_bytes()).hexdigest())

    def test_sync_error_does_not_hide_original_com_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = FakePort(observation(), OSError("original USB error"))
            with patch("gnss_logger.cli.open_port", return_value=port), \
                    patch("gnss_logger.engine.sync_sink", side_effect=OSError("sync failed")):
                code, report = self.cli(["serial", "--port", "COM99", "--capture", str(Path(tmp) / "partial.ubx"),
                                         "--session-dir", tmp])
            self.assertEqual(code, 1)
            self.assertIn("original USB error", report["error"])
            self.assertTrue(any("sync failed" in e["message"] for e in report["errors"]))
            self.assertFalse(report["artifacts"][0]["complete"])

    def test_existing_capture_is_never_touched_and_port_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "protected.ubx"
            binary.write_bytes(b"ORIGINAL")
            with patch("gnss_logger.cli.open_port") as opened:
                code, report = self.cli(["serial", "--port", "COM99", "--capture", str(binary),
                                         "--session-dir", tmp])
            self.assertEqual(code, 1)
            self.assertEqual(binary.read_bytes(), b"ORIGINAL")
            self.assertFalse(Path(str(binary) + ".meta.json").exists())
            opened.assert_not_called()

    def test_cyrillic_port_inventory_written_as_utf8_without_model_guess(self):
        from types import SimpleNamespace
        usb = SimpleNamespace(device="COM27", description="Устройство USB приёмника", hwid="USB VID:PID=1546:01A9",
            vid=0x1546, pid=0x01A9, serial_number="unknown", location="1-9", manufacturer="u-blox", product="GNSS")
        bluetooth = SimpleNamespace(device="COM5", description="Bluetooth", hwid="BTHENUM", vid=None, pid=None)
        with tempfile.TemporaryDirectory() as tmp, patch("serial.tools.list_ports.comports", return_value=[usb, bluetooth]):
            path = Path(tmp) / "ports.json"
            code, report = self.cli(["ports", "--format", "json", "--json", str(path)])
            self.assertEqual(code, 0)
            selected = next(p for p in report["ports"] if p["device"] == "COM27")
            self.assertTrue(selected["usb_candidate"])
            self.assertIsNone(selected["model"])
            self.assertEqual(selected["model_detection_status"], "NOT_QUERIED")
            self.assertIn("Устройство USB", path.read_text(encoding="utf-8"))
            self.assertFalse(next(p for p in report["ports"] if p["device"] == "COM5")["usb_candidate"])


class PhaseRegressionTests(unittest.TestCase):
    def diagnostic(self, *data):
        d = Diagnostics()
        for packet_bytes in data:
            d.feed(packet_bytes)
        return d

    def test_phase_loss_then_reacquisition_keeps_last_valid_lock(self):
        d = self.diagnostic(observation(0, lock=10000), observation(1, track=1, lock=0),
                            observation(2, lock=500))
        counts = d.report()["phase_quality"]["counts"]
        self.assertEqual(counts["phase_validity_lost"], 1)
        self.assertEqual(counts["phase_reacquired_indicator"], 1)
        self.assertEqual(counts["locktime_decrease"], 1)
        self.assertEqual(d.lock_regressions, 1)

    def test_half_cycle_invalid_is_separate_from_phase_valid(self):
        d = self.diagnostic(observation(0, track=3))
        self.assertEqual(d.phase_valid, 1)
        self.assertEqual(d.half_cycle_invalid, 1)
        self.assertEqual(d.report()["phase_quality"]["status"], "WARNINGS")

    def test_half_cycle_changes_are_logged(self):
        d = self.diagnostic(observation(0), observation(1, track=3), observation(2, track=15))
        counts = d.report()["phase_quality"]["counts"]
        self.assertEqual(counts["half_cycle_validity_changed"], 2)
        self.assertEqual(counts["half_cycle_adjustment_flag_changed"], 1)

    def test_missing_signal_and_return_reports_gap(self):
        no_meas = packet(2, 0x15, synthetic_epoch(START + timedelta(seconds=1), ())[6:-2])
        d = self.diagnostic(observation(0, lock=10000), no_meas, observation(2, lock=200))
        quality = d.report()["phase_quality"]
        self.assertEqual(quality["counts"]["channel_missing"], 1)
        self.assertEqual(quality["counts"]["observation_gap"], 1)
        gap = next(e for e in quality["events"] if e["kind"] == "observation_gap")
        self.assertEqual(gap["channel"], "0:3:0:0")
        self.assertEqual(gap["interval_s"], 2)
        self.assertEqual(gap["excess_s"], 1)
        self.assertEqual(d.rawx_count, 3)

    def test_channels_are_not_compared_across_satellites_or_signals(self):
        d = self.diagnostic(observation(0, lock=10000, sv=3), observation(1, lock=10, sv=4),
                            observation(2, lock=20, sv=4, sig=3))
        self.assertEqual(d.lock_regressions, 0)
        self.assertEqual(d.report()["phase_quality"]["counts"]["channel_missing"], 2)

    def test_clock_reset_does_not_invent_lock_regression(self):
        d = self.diagnostic(observation(0, lock=10000), observation(1, lock=10, status=3))
        self.assertEqual(d.lock_regressions, 0)
        self.assertEqual(d.report()["phase_quality"]["counts"]["receiver_clock_reset"], 1)

    def test_event_cap_does_not_lose_counts(self):
        tracker = PhaseTracker(limit=2)
        for n in range(8):
            tracker.update(rawx(observation(n, track=1 if n % 2 else 7)[6:-2]))
        report = tracker.report()
        self.assertEqual(len(report["events"]), 2)
        self.assertGreater(report["events_omitted"], 0)
        self.assertEqual(report["counts"]["phase_validity_lost"], 4)
        self.assertIn("not proven cycle slips", report["interpretation"])

    def test_measurement_bytes_never_changed_by_diagnostics(self):
        data = observation(0) + observation(1, track=3) + observation(2, lock=10)
        before = hashlib.sha256(data).hexdigest()
        self.diagnostic(data)
        self.assertEqual(before, hashlib.sha256(data).hexdigest())

