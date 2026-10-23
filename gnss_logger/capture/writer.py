from datetime import timezone
import hashlib
import os
from pathlib import Path
import re
import uuid


class UTCWriter:
    """Exclusive segment creation; never alters incoming bytes or deletes originals."""

    def __init__(self, directory, station="UBX1", simulated=False, clock=None, session_id=None):
        import time
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", station):
            raise ValueError("Station must contain 1..32 ASCII letters, digits, _ or -")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.station = station
        self.simulated = simulated
        self.clock = clock or time.monotonic
        self.session_id = session_id or uuid.uuid4().hex
        self.file = None
        self.segments = []
        self.day = None
        self.last_sync = self.clock()

    def write(self, data, timestamp):
        if not data:
            return
        if timestamp is None or timestamp.tzinfo is None:
            raise ValueError("UTC recording requires a known, timezone-aware source timestamp")
        timestamp = timestamp.astimezone(timezone.utc)
        day = timestamp.date()
        if self.file is None or day != self.day:
            self.close()
            number = len(self.segments) + 1
            label = "MOCK_" if self.simulated else ""
            name = f"{label}{self.station}_{timestamp:%Y%m%d_%H%M%S}_UTC_part{number:03d}_{self.session_id}.ubx"
            self.file = (self.directory / name).open("xb")
            self.day = day
            self.digest = hashlib.sha256()
            self.segments.append(dict(path=str((self.directory / name).resolve()),
                                      utc_day=str(day), start_utc=timestamp.isoformat(),
                                      end_utc=timestamp.isoformat(), bytes=0, status="recording",
                                      simulated=self.simulated))
        try:
            if self.file.write(data) != len(data):
                raise OSError("Short binary write")
            self.digest.update(data)
            self.segments[-1]["bytes"] += len(data)
            self.segments[-1]["end_utc"] = timestamp.isoformat()
            if self.clock() - self.last_sync >= 5:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.last_sync = self.clock()
        except BaseException:
            self.segments[-1]["status"] = "failed"
            raise

    def close(self):
        if self.file is None:
            return
        handle, self.file = self.file, None
        try:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            if self.segments[-1]["status"] != "failed":
                self.segments[-1]["status"] = "closed"
                self.segments[-1]["sha256"] = self.digest.hexdigest()
        except BaseException:
            self.segments[-1]["status"] = "failed"
            handle.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
