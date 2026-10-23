"""Synthetic fixtures only: none of these values are device measurements."""

from collections import deque
from datetime import datetime, timedelta, timezone
import struct

from .device.adapters import F9PAdapter, X20PAdapter
from .device.requests import ensure_read_only
from .device.ubx import UBXParser, packet, valget, value_size
from .sources import Chunk
from .timebase import GPS_EPOCH


PROFILES = {
    "f9p": dict(model="ZED-F9P", firmware="MOCK-F9P", protocol="MOCK", adapter=F9PAdapter,
                signal_ids=(0, 3)),
    "x20p": dict(model="ZED-X20P", firmware="MOCK-X20P", protocol="MOCK", adapter=X20PAdapter,
                 signal_ids=(0, 3, 6)),
}


def synthetic_epoch(timestamp, signal_ids=(0, 3), lock_ms=1000):
    gps_seconds = (timestamp - GPS_EPOCH).total_seconds() + 18
    week, tow = divmod(gps_seconds, 604800)
    header = struct.pack("<dHbBBBBB", tow, int(week), 18, len(signal_ids), 1, 1, 0, 0)
    measurements = b"".join(struct.pack("<ddfBBBBHBBBBBB", 21000000.0, 110000000.5,
        -200.0, 0, 3, sig, 0, min(lock_ms, 64500), 45, 1, 2, 3, 7, 0) for sig in signal_ids)
    return packet(2, 0x15, header + measurements)


def synthetic_nav():
    # Framing fixture, not usable ephemeris/navigation content.
    return packet(2, 0x13, bytes((0, 3, 0, 0, 1, 0, 2, 0)) + bytes(4))


class MockReceiver:
    kind = "mock"
    simulated = True
    finite = True
    time_basis = "synthetic_UTC; leapS=18 is a test constant"

    def __init__(self, profile="f9p", epochs=5, scenario="normal", start_utc=None):
        self.profile = PROFILES[profile]
        self.epochs = epochs
        self.scenario = scenario
        self.start_utc = start_utc or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.now = 0.0
        self.responses = deque()
        self.writes = []

    def monotonic(self):
        return self.now

    def send(self, data):
        ensure_read_only(data)
        self.writes.append(data)
        cls, msg, payload = UBXParser().feed(data)[0]
        if self.scenario == "timeout":
            return len(data)
        if (cls, msg) == (10, 4):
            def text(s, n):
                return s.encode("ascii").ljust(n, b"\0")
            identity = text("SIMULATED RECEIVER", 30) + text("MOCK", 10)
            for item in ("MOD=" + self.profile["model"], "FWVER=" + self.profile["firmware"], "PROTVER=MOCK"):
                identity += text(item, 30)
            response = packet(10, 4, identity)
        else:
            query = valget(payload)
            fixture = {self.profile["adapter"].keys["RATE-MEAS"]: 1000,
                       self.profile["adapter"].keys["RAWX-USB"]: 1,
                       self.profile["adapter"].keys["SFRBX-USB"]: 1}
            if self.scenario == "nak" or any(k not in fixture for k in query["keys"]):
                self.responses.append(packet(5, 0, bytes((cls, msg))))
                return len(data)
            if self.scenario in ("ack", "ack-only"):
                self.responses.append(packet(5, 1, bytes((cls, msg))))
                if self.scenario == "ack-only":
                    return len(data)
            body = struct.pack("<BBH", 1, query["layer"], query["position"])
            for key in query["keys"]:
                body += struct.pack("<I", key) + fixture[key].to_bytes(value_size(key), "little")
            response = packet(6, 0x8B, body)
        if self.scenario == "corrupt":
            response = response[:-1] + bytes((response[-1] ^ 1,))
        elif self.scenario == "malformed":
            response = packet(cls, msg, b"\x01")
        if self.scenario == "partial":
            self.responses.extend(response[i:i + 3] for i in range(0, len(response), 3))
        else:
            self.responses.append(response)
        return len(data)

    def __iter__(self):
        for index in range(self.epochs + 4):
            self.now = float(index)
            # Drain fragmented replies at the same virtual instant; no real sleep.
            while self.responses:
                yield Chunk(self.responses.popleft(), self.start_utc + timedelta(seconds=index))
            if index < self.epochs and self.scenario != "no-data":
                yield Chunk(synthetic_epoch(self.start_utc + timedelta(seconds=index),
                            self.profile["signal_ids"], (index + 1) * 1000) + synthetic_nav(),
                            self.start_utc + timedelta(seconds=index))
            else:
                yield Chunk(b"", self.start_utc + timedelta(seconds=index))
