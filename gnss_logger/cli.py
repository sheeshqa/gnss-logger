import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

from . import __version__
from .capture.writer import UTCWriter
from .device.ports import port_info
from .device.requests import Probe
from .device.transport import open_port, send_read_only
from .diagnostics import Diagnostics
from .engine import RunState, run_source
from .mock import MockReceiver
from .session import CaptureFile, Session, file_fingerprint, utc_now, write_new_json
from .sources import ReplaySource, SerialSource
from .timebase import parse_utc


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and > 0")
    return result


def speed(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return result


def utc_argument(value):
    try:
        return parse_utc(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def read_stream(connection, diagnostics, seconds, capture=None, request_version=False,
                clock=time.monotonic, progress=None):
    probe = Probe(lambda data: send_read_only(connection, data)) if request_version else None
    return run_source(SerialSource(connection), diagnostics, seconds, capture=capture,
                      probe=probe, clock=clock, progress=progress)


def parser():
    p = argparse.ArgumentParser(description="Read-only GNSS diagnostics: serial / replay / simulated mock")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    ports = sub.add_parser("ports", help="List USB identities without opening any port")
    ports.add_argument("--format", choices=("text", "json"), default="text")
    live = sub.add_parser("serial", aliases=["diagnose"], help="Read one explicitly selected COM port")
    live.add_argument("--port", required=True)
    live.add_argument("--baud", type=int, default=115200, help="Host baud only; receiver settings unchanged")
    live.add_argument("--seconds", type=positive, default=60)
    live.add_argument("--poll-mon-ver", action="store_true")
    live.add_argument("--inspect", action="store_true", help="Read MON-VER and three RAM/USB config keys")
    live.add_argument("--request-timeout", type=positive, default=2)
    replay = sub.add_parser("replay", help="Read saved bytes; never transmits logged commands")
    replay.add_argument("file", type=Path)
    replay.add_argument("--speed", type=speed, default=0, help="0=unpaced, 1=real-time, N=acceleration")
    replay.add_argument("--start-utc", type=utc_argument)
    mock = sub.add_parser("mock", help="SIMULATED F9P/X20P; no COM access")
    mock.add_argument("--profile", choices=("f9p", "x20p"), default="f9p")
    mock.add_argument("--epochs", type=int, default=5)
    mock.add_argument("--scenario", choices=("normal", "timeout", "nak", "ack", "ack-only", "partial",
                                           "corrupt", "malformed", "no-data"), default="normal")
    mock.add_argument("--start-utc", type=utc_argument)
    mock.add_argument("--request-timeout", type=positive, default=2)
    for command in (ports, live, replay, mock):
        command.add_argument("--session-dir", type=Path, help="Parent of a new unique per-attempt audit directory")
        command.add_argument("--json", type=Path, help="Optional additional report file; never overwrites")
    for command in (live, replay, mock):
        command.add_argument("--expected-hz", type=positive, default=1)
        command.add_argument("--capture", type=Path, help="New exact binary stream file")
        command.add_argument("--record-dir", type=Path, help="Existing Stage 1 UTC segmentation prototype")
        command.add_argument("--station", default="UBX1")
    from .recording.cli import add_commands
    add_commands(sub, positive)
    return p


def progress(d):
    print(f"bytes={d.bytes_received} RAWX={d.rawx_count} SFRBX={d.sfrbx_count} "
          f"checksum_errors={d.parser.bad_checksums}", file=sys.stderr, flush=True)


def result_code(d, probe):
    if probe and (probe.pending or any(r["status"] in ("nak", "timeout") for r in probe.results)):
        return 3
    if not d.rawx_count or not d.sfrbx_count or not d.phase_valid or not d.code_valid:
        return 2
    if (d.decode_errors or d.parser.bad_checksums or d.parser.oversize_headers or d.gap_events
        or d.nonmonotonic_epochs or (d.liveness and
            (d.liveness["rawx_stale"] or d.liveness["sfrbx_stale"]))):
        return 3
    return 0


def json_parameters(args):
    return {key: value.isoformat() if isinstance(value, datetime) else
            str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}


def main(argv=None):
    # Files always use UTF-8; also fix Python -> PowerShell pipes without changing global settings.
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="backslashreplace")
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    args = p.parse_args(actual_argv)
    from .recording.cli import COMMANDS, preview
    if args.command in COMMANDS:
        return preview(args, p)
    is_serial = args.command in ("serial", "diagnose")
    if is_serial and args.baud <= 0:
        p.error("--baud must be > 0")
    if args.command == "mock" and args.epochs <= 0:
        p.error("--epochs must be > 0")
    d = Diagnostics(getattr(args, "expected_hz", 1))
    state = RunState()
    report = dict(schema_version=2, session_id=uuid.uuid4().hex, tool_version=__version__, started_utc=utc_now(),
        source=str(args.file if args.command == "replay" else args.port if is_serial else
                   args.profile if args.command == "mock" else "port_inventory"),
        mode="serial" if is_serial else args.command, simulated=args.command == "mock",
        provenance="synthetic_fixture" if args.command == "mock" else
                   "saved_file_not_live_hardware" if args.command == "replay" else
                   "physical_port_attempt" if is_serial else "port_inventory_only",
        host_baud=getattr(args, "baud", None), errors=[],
        connection=dict(port=args.port, baudrate=args.baud, bytesize=8, parity="N", stopbits=1,
                        configured_read_timeout_s=0.25, write_timeout_s=1,
                        xonxoff=False, rtscts=False, dsrdtr=False) if is_serial else None)
    started = time.monotonic()
    code = 0
    session = external = capture = writer = probe = source = None
    artifacts = []
    operation = "session_setup"

    def record_error(component, error, include_trace=False):
        entry = dict(utc=utc_now(), component=component, type=type(error).__name__, message=str(error))
        if include_trace:
            entry["traceback"] = traceback.format_exc()
        report["errors"].append(entry)
        if session:
            try:
                session.event("error", detail=entry)
            except Exception:
                pass  # Disk failure may also prevent journal writes; stdout remains available.

    try:
        parent = args.session_dir or (args.json.parent / "sessions" if args.json else Path("reports/sessions"))
        session = Session(parent, actual_argv, json_parameters(args), session_id=report["session_id"])
        report.update(session.base)
        operation = "output_setup"
        if args.json:
            external = args.json.open("x", encoding="utf-8", newline="\n")
        capture_path = getattr(args, "capture", None)
        if capture_path:
            if Path(str(capture_path) + ".meta.json").exists():
                raise FileExistsError("Capture sidecar already exists; select a new capture path")
            capture = CaptureFile(capture_path)  # Exclusive before port open.
        with ExitStack() as stack:
            operation = "source_open"
            if args.command == "ports":
                from serial.tools.list_ports import comports
                report["ports"] = [port_info(port) for port in sorted(comports(), key=lambda port: port.device)]
                state.stop_reason = "port_inventory_complete"
            else:
                if is_serial:
                    try:
                        connection = stack.enter_context(open_port(args.port, args.baud))
                    except Exception as error:
                        raise OSError(f"Не удалось открыть {args.port}. Порт отсутствует, занят или недоступен; "
                                      f"доступны replay и mock. Причина: {error}") from error
                    source = SerialSource(connection)
                    if args.poll_mon_ver or args.inspect:
                        probe = Probe(lambda data: send_read_only(connection, data), args.request_timeout, args.inspect)
                    run_clock = time.monotonic
                elif args.command == "replay":
                    source = ReplaySource(args.file, args.speed, args.start_utc)
                    run_clock = time.monotonic
                else:
                    source = MockReceiver(args.profile, args.epochs, args.scenario, args.start_utc)
                    probe = Probe(source.send, args.request_timeout, inspect=True)
                    run_clock = source.monotonic
                report["simulated"] = source.simulated
                report["original_provenance"] = getattr(source, "original_provenance", report["provenance"])
                if args.record_dir:
                    writer = stack.enter_context(UTCWriter(args.record_dir, args.station, source.simulated,
                                                          session_id=session.id))
                session.event("source_opened", mode=report["mode"], source=report["source"],
                              simulated=report["simulated"])

                def log_progress(diag):
                    progress(diag)
                    session.event("progress", bytes_received=diag.bytes_received,
                                  rawx=diag.rawx_count, sfrbx=diag.sfrbx_count)

                operation = "run"
                run_source(source, d, seconds=args.seconds if is_serial else None, capture=capture,
                           writer=writer, probe=probe, clock=run_clock, progress=log_progress, state=state)
                operation = "connection_close"
    except KeyboardInterrupt as error:
        state.stop_reason = "keyboard_interrupt"
        report["interrupted"] = True
        record_error(operation, error)
        code = 130
    except Exception as error:
        if state.stop_reason in ("not_started", "deadline", "source_exhausted", "port_inventory_complete"):
            state.stop_reason = operation + "_error"
        report["error"] = f"{type(error).__name__}: {error}"
        record_error(operation, error, include_trace=True)
        code = 1
    finally:
        for error in state.finalization_errors:
            record_error(error["component"], OSError(error["error"]))
            code = 1
        normal = state.stop_reason in ("deadline", "source_exhausted", "port_inventory_complete")
        if capture is not None:
            artifact = capture.finalize(normal and code == 0 and state.bytes_received > 0,
                                        state.stop_reason, state.report())
            artifacts.append(artifact)
            if artifact["errors"] or not artifact["matches_received_stream"]:
                record_error("capture_finalize", OSError(str(artifact)))
                code = 1
        if writer:
            for index, segment in enumerate(writer.segments):
                artifact = dict(segment, kind="utc_segment", state="closed")
                try:
                    artifact.update(file_fingerprint(segment["path"]))
                    artifact["complete"] = (segment.get("status") == "closed" and
                        (index < len(writer.segments) - 1 or normal) and code == 0)
                except Exception as error:
                    artifact.update(complete=False, state="unreadable")
                    record_error("segment_finalize", error)
                    code = 1
                artifacts.append(artifact)

    report.update(d.report())
    report["schema_version"] = 2
    report.update(run=state.report(), stream_sha256=state.digest.hexdigest(),
                  stop_reason=state.stop_reason, ended_utc=utc_now(),
                  elapsed_s=time.monotonic() - started,
                  source_time_basis=getattr(source, "time_basis", None),
                  requests=probe.results if probe else [], request_pending=probe.pending if probe else None,
                  segments=writer.segments if writer else [], artifacts=artifacts)
    if code == 0 and args.command != "ports":
        code = result_code(d, probe)
    complete = state.stop_reason in ("deadline", "source_exhausted", "port_inventory_complete") and not report["errors"]
    status = "SUCCESS" if complete and (state.bytes_received or args.command == "ports") else (
        "NO_DATA" if complete else "PARTIAL" if state.bytes_received else "FAILED")
    report["registration"] = dict(status=status, complete=complete, received_bytes=state.bytes_received,
        captured_bytes=sum(a.get("bytes") or 0 for a in artifacts if a["kind"] == "capture"),
        capture_requested=capture is not None, file_states=[a["status"] if "status" in a else a["state"] for a in artifacts])
    # Partial artifacts always get a manifest, including source-open/USB/Ctrl+C failures.
    for artifact in artifacts:
        try:
            write_new_json(artifact["path"] + ".meta.json",
                dict(schema_version=2, session_id=report.get("session_id"), simulated=report["simulated"],
                     mode=report["mode"], source=report["source"], provenance=report["provenance"],
                     source_time_basis=report["source_time_basis"], artifact=artifact,
                     stop_reason=report["stop_reason"], complete=artifact.get("complete", False),
                     last_successful_read_utc=state.last_successful_read_utc,
                     last_data_read_utc=state.last_data_read_utc))
        except Exception as error:
            record_error("sidecar", error)
            code = 1
    report["exit_code"] = code
    report["result"] = {0: "completed_not_scientific_or_hardware_acceptance", 1: "error",
                       2: "insufficient_observations", 3: "protocol_or_request_issues", 130: "interrupted"}[code]
    if report["errors"] and report["registration"]["complete"]:
        report["registration"].update(status="PARTIAL" if state.bytes_received else "FAILED", complete=False)
    if session:
        report["paths"] = session.paths(artifacts)
    # Mirror first; any mirror failure is captured in the canonical session report.
    if external is not None:
        try:
            json.dump(report, external, ensure_ascii=False, indent=2, allow_nan=False)
            external.write("\n")
            external.flush()
            os.fsync(external.fileno())
        except Exception as error:
            record_error("external_report", error)
            code = report["exit_code"] = 1
            report["result"] = "error"
            report["registration"].update(status="PARTIAL" if state.bytes_received else "FAILED", complete=False)
        finally:
            try:
                external.close()
            except Exception as error:
                record_error("external_report_close", error)
                code = report["exit_code"] = 1
    try:
        if session:
            session.finish(report, artifacts)
    except Exception as error:
        record_error("session_finalize", error)
        code = report["exit_code"] = 1
        report["result"] = "error"
    finally:
        if session:
            try:
                session.close()
            except Exception as error:
                record_error("journal_close", error)
                code = report["exit_code"] = 1
    if args.command == "ports" and args.format == "text" and code == 0:
        for port in report.get("ports", []):
            print(f"{port['device']}\t{port['description']}\t{port['hwid']}\t{port['hint'] or ''}")
        if not report.get("ports"):
            print("COM-порты не найдены. Доступны replay и mock.")
        print(f"Session report: {report['paths']['report']}", file=sys.stderr)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return code

