"""Pure contracts and invariants; importing this module performs no I/O."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence
import math
import re


class State(str, Enum):
    STARTING = "STARTING"
    IDENTIFYING = "IDENTIFYING"
    RECORDING = "RECORDING"
    DISCONNECTED = "DISCONNECTED"
    WAITING = "WAITING"
    STORAGE_ERROR = "STORAGE_ERROR"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


_NEXT = {
    State.STARTING: {State.IDENTIFYING, State.STORAGE_ERROR, State.STOPPING},
    State.IDENTIFYING: {State.RECORDING, State.DISCONNECTED, State.WAITING, State.STORAGE_ERROR, State.STOPPING},
    State.RECORDING: {State.DISCONNECTED, State.STORAGE_ERROR, State.STOPPING},
    State.DISCONNECTED: {State.WAITING, State.STORAGE_ERROR, State.STOPPING},
    State.WAITING: {State.IDENTIFYING, State.STORAGE_ERROR, State.STOPPING},
    State.STORAGE_ERROR: {State.STOPPING},
    State.STOPPING: {State.STOPPED},
    State.STOPPED: set(),
}


def require_transition(before: State, after: State) -> None:
    """An accepted transition requires a persisted event in the future controller."""
    if not isinstance(before, State) or not isinstance(after, State) or after not in _NEXT[before]:
        raise ValueError(f"Illegal recording transition: {before} -> {after}")


def uint(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("UTC must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Wire timestamps must explicitly use UTC")
    return parsed.astimezone(timezone.utc)


def identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Identifier must be a lowercase UUID hex string")


@dataclass(frozen=True)
class ByteAccounting:
    received: int
    journal_written: int
    journal_synced: int
    pending: int
    unarchived: int
    framed: int
    opaque: int
    unclassified: int
    received_count_exact: bool

    def __post_init__(self):
        for name in ("received", "journal_written", "journal_synced", "pending", "unarchived", "framed", "opaque", "unclassified"):
            uint(getattr(self, name), name)
        if type(self.received_count_exact) is not bool:
            raise ValueError("received_count_exact must be boolean")
        if self.received != self.journal_written + self.pending + self.unarchived:
            raise ValueError("Every observed received byte must be accounted for")
        if self.journal_synced > self.journal_written:
            raise ValueError("Synced bytes cannot exceed written bytes")
        if self.journal_written != self.framed + self.opaque + self.unclassified:
            raise ValueError("Journal byte classification must cover its written prefix")


@dataclass(frozen=True)
class InputChunk:
    connection_id: str
    sequence: int
    offset: int
    data: bytes
    received_utc: str
    monotonic_ns: int

    def __post_init__(self):
        identifier(self.connection_id)
        for name in ("sequence", "offset", "monotonic_ns"):
            uint(getattr(self, name), name)
        if not isinstance(self.data, bytes):
            raise ValueError("Input data must be immutable bytes")
        utc(self.received_utc)


@dataclass(frozen=True)
class DeviceBinding:
    model: str
    vid: int
    pid: int
    usb_serial: Optional[str] = None
    usb_location: Optional[str] = None

    def __post_init__(self):
        if self.model not in ("ZED-F9P", "ZED-X20P"):
            raise ValueError("An explicitly supported receiver model is required")
        for name in ("vid", "pid"):
            value = getattr(self, name)
            uint(value, name)
            if value > 65535:
                raise ValueError("USB VID/PID must fit uint16")
        selectors = (self.usb_serial, self.usb_location)
        if sum(value is not None for value in selectors) != 1:
            raise ValueError("Choose exactly one USB serial or location binding")
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in selectors):
            raise ValueError("USB selector must be non-empty")

    def matches(self, observed: Mapping) -> bool:
        """Pure check AFTER MON-VER; COM number and descriptions are never identity."""
        fields = dict(model=self.model, vid=self.vid, pid=self.pid)
        fields["usb_serial" if self.usb_serial is not None else "usb_location"] = (
            self.usb_serial if self.usb_serial is not None else self.usb_location)
        return all(type(observed.get(k)) is type(v) and observed.get(k) == v for k, v in fields.items())


def require_unique_match(binding: DeviceBinding, observed: Sequence[Mapping]) -> Mapping:
    """Ambiguity is an error; this pure check never discovers or opens devices."""
    matches = [item for item in observed if binding.matches(item)]
    if len(matches) != 1:
        raise ValueError("Exactly one verified receiver must match the explicit binding")
    return matches[0]


@dataclass(frozen=True)
class TimeBinding:
    status: str  # UNDATED, VALID, DISCONTINUITY
    receiver_utc: Optional[str]
    source: str  # NONE or RAWX
    leap_seconds_valid: bool
    generation: int

    def __post_init__(self):
        uint(self.generation, "generation")
        if type(self.leap_seconds_valid) is not bool:
            raise ValueError("leap_seconds_valid must be boolean")
        if self.status == "VALID":
            if self.source != "RAWX" or not self.leap_seconds_valid or self.receiver_utc is None:
                raise ValueError("VALID UTC requires validated RAWX provenance")
            utc(self.receiver_utc)
        elif self.status in ("UNDATED", "DISCONTINUITY"):
            if self.receiver_utc is not None or self.source != "NONE" or self.leap_seconds_valid:
                raise ValueError("An unbound time must not carry a trusted timestamp")
        else:
            raise ValueError("Unknown time status")


@dataclass(frozen=True)
class RecordSettings:
    output: Path
    initial_port: str
    binding: DeviceBinding
    seconds: Optional[float]  # None means explicitly requested until-stopped.
    baud: int = 115200
    expected_hz: float = 1.0
    station: str = "UBX1"
    retention_policy: str = "keep_all"

    def __post_init__(self):
        if not isinstance(self.output, Path) or not self.initial_port.strip() or not isinstance(self.binding, DeviceBinding):
            raise ValueError("Output, initial port and binding are required")
        if self.seconds is not None and (type(self.seconds) not in (float, int) or not math.isfinite(self.seconds) or self.seconds <= 0):
            raise ValueError("Duration must be finite and positive")
        if type(self.baud) is not int or self.baud <= 0:
            raise ValueError("Baud must be positive")
        if type(self.expected_hz) not in (int, float) or not math.isfinite(self.expected_hz) or self.expected_hz <= 0:
            raise ValueError("Expected rate must be positive")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.station):
            raise ValueError("Invalid station identifier")
        if self.retention_policy != "keep_all":
            raise ValueError("Stage 2 cannot automatically delete source data")


@dataclass(frozen=True)
class WriteReceipt:
    accepted_bytes: int  # May be a prefix; caller must retain the remainder.
    journal_end: int

    def __post_init__(self):
        uint(self.accepted_bytes, "accepted_bytes")
        uint(self.journal_end, "journal_end")
        if self.accepted_bytes > self.journal_end:
            raise ValueError("Accepted prefix exceeds journal position")


@dataclass(frozen=True)
class RawRecord:
    journal_id: str
    offset: int
    data: bytes
    kind: str  # FRAME / OPAQUE / TAIL; full original bytes, never re-encoded.

    def __post_init__(self):
        identifier(self.journal_id)
        uint(self.offset, "offset")
        if self.kind not in ("FRAME", "OPAQUE", "TAIL") or not isinstance(self.data, bytes) or not self.data:
            raise ValueError("Invalid original record")


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class Controller(Protocol):
    def run(self, settings: RecordSettings, stop: StopSignal) -> Mapping: ...
    def request_stop(self, reason: str) -> None: ...
    def snapshot(self) -> Mapping: ...


class Reader(Protocol):
    def read(self, remaining_s: Optional[float]) -> InputChunk: ...
    def close(self) -> None: ...


class Journal(Protocol):
    def append(self, chunk: InputChunk) -> WriteReceipt: ...
    def checkpoint(self) -> Mapping: ...
    def close(self, reason: str) -> Mapping: ...


class Rotator(Protocol):
    def accept(self, record: RawRecord, binding: TimeBinding) -> None: ...
    def close(self, reason: str) -> Sequence[Mapping]: ...


class Reconnector(Protocol):
    def candidates(self, binding: DeviceBinding) -> Sequence[Mapping]: ...
    def identify(self, candidate: Mapping, timeout_s: float) -> Mapping: ...
    def next_delay(self, failed_attempts: int) -> float: ...


class Recovery(Protocol):
    def inspect(self, source: Path) -> Mapping: ...
    def recover(self, source: Path, new_destination: Path) -> Mapping: ...
