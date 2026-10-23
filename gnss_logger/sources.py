"""Three source modes; none owns diagnostic or receiver-configuration logic."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import time

from .device.ubx import UBXParser, mon_ver, rawx
from .timebase import rawx_utc


@dataclass
class Chunk:
    data: bytes
    utc: object = None


def file_records(path):
    """Preserve junk, damaged packets and EOF fragments as well as valid frames."""
    parser = UBXParser()
    with Path(path).open("rb") as stream:
        while data := stream.read(65536):
            yield from parser.feed_records(data)
    if parser.buffer:
        yield bytes(parser.buffer), None


def epoch_time(frame):
    if frame and frame[:2] == (2, 0x15):
        try:
            epoch = rawx(frame[2])
            return epoch["week"] * 604800 + epoch["tow_s"], rawx_utc(epoch)
        except ValueError:
            pass
    return None, None


class ReplaySource:
    kind = "replay"
    simulated = False
    finite = True

    def __init__(self, path, speed=0, start_utc=None, clock=time.monotonic, sleep=time.sleep):
        self.path = Path(path)
        self.speed = speed
        if speed < 0:
            raise ValueError("Replay speed must be >= 0 (0 = no delays)")
        self.start_utc = start_utc
        self.clock, self.sleep = clock, sleep
        self.time_basis = "receiver_RAWX_week_TOW_leapS_valid; not independent UTC verification"
        self.simulated = False
        self.original_provenance = "unverified_saved_bytes"
        sidecar = Path(str(self.path) + ".meta.json")
        if sidecar.exists():
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            if metadata.get("simulated") is True:
                self.simulated = True
                self.original_provenance = "synthetic_fixture_sidecar"
        self.first_gps = self.first_utc = None
        for _, frame in file_records(self.path):
            if frame and frame[:2] == (10, 4) and frame[2]:
                try:
                    identity = mon_ver(frame[2])
                    if (identity["software"].startswith("SIMULATED") or
                            (identity.get("firmware") or "").startswith("MOCK")):
                        self.simulated = True
                        self.original_provenance = "synthetic_MON_VER_marker"
                except ValueError:
                    pass
            gps, utc = epoch_time(frame)
            if gps is not None:
                self.first_gps, self.first_utc = gps, utc
                break

    def __iter__(self):
        first_gps, first_utc = self.first_gps, self.first_utc
        if self.start_utc is not None:
            first_utc = self.start_utc
            self.time_basis = "user_supplied_replay_UTC_anchor"
        elif first_utc is None:
            self.time_basis = "unknown; use --start-utc to enable recording without valid RAWX time"
        current_utc = first_utc
        wall_start = self.clock()
        for original, frame in file_records(self.path):
            gps, utc = epoch_time(frame)
            if gps is not None:
                if self.start_utc is not None:
                    current_utc = first_utc + timedelta(seconds=gps - first_gps)
                else:
                    # Do not silently carry a known UTC across an invalid RAWX time.
                    current_utc = utc
                if self.speed and first_gps is not None:
                    target = wall_start + max(0, gps - first_gps) / self.speed
                    # Short sleeps remain interruptible even for multi-day gaps.
                    while self.clock() < target:
                        self.sleep(max(0, min(0.25, target - self.clock())))
            yield Chunk(original, current_utc)


class SerialSource:
    kind = "serial"
    simulated = False
    finite = False
    time_basis = "host_arrival_UTC (computer clock; USB buffering applies)"

    def __init__(self, connection):
        self.connection = connection
        self.original_timeout = getattr(connection, "timeout", 0.25)

    def set_read_timeout(self, remaining):
        baseline = self.original_timeout
        timeout = (max(0, remaining if baseline is None else min(baseline, remaining))
                   if remaining is not None else baseline)
        if getattr(self.connection, "timeout", None) != timeout:
            self.connection.timeout = timeout

    def restore_read_timeout(self):
        if getattr(self.connection, "timeout", None) != self.original_timeout:
            self.connection.timeout = self.original_timeout

    def __iter__(self):
        while True:
            yield Chunk(self.connection.read(4096), datetime.now(timezone.utc))
