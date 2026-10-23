"""Per-attempt audit bundle and read-back verification of files. No recovery queue."""

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys
import uuid

from . import __version__


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_new_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def file_fingerprint(path):
    digest = hashlib.sha256()
    count = 0
    with Path(path).open("rb") as stream:
        while data := stream.read(65536):
            count += len(data)
            digest.update(data)
    return dict(bytes=count, sha256=digest.hexdigest())


def versions():
    dependencies = {}
    for name in ("pyserial",):
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    return dict(python=sys.version, executable=sys.executable, platform=platform.platform(),
                gnss_logger=__version__, dependencies=dependencies)


class Session:
    def __init__(self, parent, argv, parameters, session_id=None):
        self.id = session_id or uuid.uuid4().hex
        self.started_utc = utc_now()
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.directory = parent / (stamp + "_" + self.id)
        self.directory.mkdir(exist_ok=False)
        self.journal_path = self.directory / "events.jsonl"
        self.journal = self.journal_path.open("x", encoding="utf-8", newline="\n")
        self.base = dict(session_id=self.id, started_utc=self.started_utc, argv=list(argv),
                         parameters=parameters, versions=versions(),
                         hardware_test_status="NOT TESTED",
                         hardware_validation=dict(status="NOT TESTED", software_version=__version__,
                           scope="No automatic hardware acceptance; this software version requires manual retest"))
        self.event("session_started", **self.base)

    def event(self, event, **fields):
        self.journal.write(json.dumps(dict(utc=utc_now(), event=event, **fields),
                                      ensure_ascii=False, allow_nan=False) + "\n")
        self.journal.flush()

    def paths(self, artifacts):
        return dict(session_directory=str(self.directory.resolve()),
                    report=str((self.directory / "report.json").resolve()),
                    manifest=str((self.directory / "manifest.json").resolve()),
                    events=str(self.journal_path.resolve()), artifacts=[a["path"] for a in artifacts])

    def finish(self, report, artifacts):
        report["paths"] = self.paths(artifacts)
        self.event("session_finished", exit_code=report["exit_code"],
                   stop_reason=report["stop_reason"], registration=report["registration"])
        manifest = dict(schema_version=1, session_id=self.id, started_utc=report["started_utc"],
                        ended_utc=report["ended_utc"], stop_reason=report["stop_reason"],
                        exit_code=report["exit_code"], simulated=report.get("simulated", False),
                        provenance=report.get("provenance"), run=report.get("run"),
                        registration=report["registration"], artifacts=artifacts,
                        phase_quality_status=report.get("phase_quality", {}).get("status"),
                        paths=report["paths"], parameters=report["parameters"],
                        versions=report["versions"], errors=report.get("errors", []))
        write_new_json(self.directory / "manifest.json", manifest)
        write_new_json(self.directory / "report.json", report)

    def close(self):
        if not self.journal.closed:
            self.journal.close()


class CaptureFile:
    def __init__(self, path):
        self.path = Path(path)
        # Unbuffered binary sink: accepted prefixes are countable; fsync still periodic.
        self.handle = self.path.open("xb", buffering=0)
        self.write_bytes = 0
        self.errors = []
        self.closed = False

    def write(self, data):
        count = self.handle.write(data)
        if count is not None:
            self.write_bytes += count
        return count

    def flush(self):
        self.handle.flush()

    def fileno(self):
        return self.handle.fileno()

    def finalize(self, complete, reason, received):
        for name, action in (("flush", self.handle.flush),
                             ("fsync", lambda: os.fsync(self.handle.fileno())),
                             ("close", self.handle.close)):
            try:
                action()
            except Exception as error:
                self.errors.append(dict(component=name, error=f"{type(error).__name__}: {error}"))
        self.closed = self.handle.closed
        result = dict(path=str(self.path.resolve()), kind="capture", stop_reason=reason,
                      state="closed" if self.closed else "close_failed", errors=list(self.errors))
        try:
            result.update(file_fingerprint(self.path))
            result["matches_received_stream"] = (result["bytes"] == received["bytes_received"] and
                                                result["sha256"] == received["stream_sha256"])
            result["unarchived_bytes"] = max(0, received["bytes_received"] - result["bytes"])
        except Exception as error:
            result.update(bytes=None, sha256=None, matches_received_stream=False, unarchived_bytes=None)
            result["errors"].append(dict(component="readback", error=f"{type(error).__name__}: {error}"))
        result["complete"] = bool(complete and self.closed and not result["errors"] and
                                  result["matches_received_stream"])
        result["status"] = "closed_complete" if result["complete"] else "closed_partial" if self.closed else "close_failed"
        return result

