"""One reader; returned bytes are processed even when a read crosses the deadline."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import os
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunState:
    bytes_received: int = 0
    read_calls: int = 0
    empty_reads: int = 0
    stop_reason: str = "not_started"
    last_successful_read_utc: object = None
    last_data_read_utc: object = None
    deadline_overrun_s: float = 0.0
    timeout_bounded: bool = False
    finalization_errors: list = field(default_factory=list)
    digest: object = field(default_factory=hashlib.sha256, repr=False)

    def report(self):
        return dict(bytes_received=self.bytes_received, read_calls=self.read_calls,
                    empty_reads=self.empty_reads, stream_sha256=self.digest.hexdigest(),
                    stop_reason=self.stop_reason,
                    last_successful_read_utc=self.last_successful_read_utc,
                    last_data_read_utc=self.last_data_read_utc,
                    deadline_overrun_s=self.deadline_overrun_s,
                    source_read_timeout_bounded=self.timeout_bounded,
                    finalization_errors=list(self.finalization_errors))


def sync_sink(sink):
    sink.flush()
    try:
        descriptor = sink.fileno()
    except io.UnsupportedOperation:
        return  # BytesIO test sink; actual file errors still propagate.
    os.fsync(descriptor)


def run_source(source, diagnostics, seconds=None, capture=None, writer=None, probe=None,
               clock=time.monotonic, progress=None, state=None, utc_clock=utc_now):
    state = state if state is not None else RunState()
    start = clock()
    deadline = start + seconds if seconds is not None else None
    last_progress = last_sync = start
    last_rawx = last_sfrbx = None
    operation = "source"
    state.stop_reason = "running"
    primary_error = False
    try:
        iterator = iter(source)
        if probe:
            operation = "request"
            probe.start(start)
        while True:
            # Check BEFORE next(): an iterator may block while producing a chunk.
            before = clock()
            remaining = None if deadline is None else deadline - before
            if remaining is not None and remaining <= 0:
                state.stop_reason = "deadline"
                break
            if hasattr(source, "set_read_timeout"):
                source.set_read_timeout(remaining)
                state.timeout_bounded = True
            operation = "source"
            try:
                chunk = next(iterator)
            except StopIteration:
                state.stop_reason = "source_exhausted"
                break
            now = clock()
            state.read_calls += 1
            state.last_successful_read_utc = utc_clock()
            if deadline is not None:
                state.deadline_overrun_s = max(state.deadline_overrun_s, max(0, now - deadline))
            # No deadline break here: ALL bytes already returned belong to this capture.
            if chunk.data:
                state.bytes_received += len(chunk.data)
                state.digest.update(chunk.data)
                state.last_data_read_utc = state.last_successful_read_utc
                operation = "processing"
                before_counts = diagnostics.rawx_count, diagnostics.sfrbx_count
                frames = diagnostics.feed(chunk.data)
                operation = "capture"
                if capture is not None and capture.write(chunk.data) != len(chunk.data):
                    raise OSError("Short capture write; received and saved byte counts may differ")
                if writer is not None:
                    writer.write(chunk.data, chunk.utc)
                if diagnostics.rawx_count != before_counts[0]:
                    last_rawx = now
                if diagnostics.sfrbx_count != before_counts[1]:
                    last_sfrbx = now
                operation = "request"
                if probe:
                    probe.tick(now)
                    for frame in frames:
                        try:
                            probe.handle(frame, now)
                        except ValueError:
                            pass  # Diagnostics recorded the malformed response.
            else:
                state.empty_reads += 1
                if probe:
                    probe.tick(now)
            if not source.finite:
                diagnostics.liveness = dict(
                    rawx_age_s=now - last_rawx if last_rawx is not None else None,
                    sfrbx_age_s=now - last_sfrbx if last_sfrbx is not None else None,
                    rawx_stale=last_rawx is not None and now - last_rawx > max(5, 3 / diagnostics.expected_hz),
                    sfrbx_stale=last_sfrbx is not None and now - last_sfrbx > 30,
                    sfrbx_stale_threshold_s=30)
            operation = "capture"
            if capture is not None and now - last_sync >= 5:
                sync_sink(capture)
                last_sync = now
            operation = "progress"
            if progress and now - last_progress >= 5:
                progress(diagnostics)
                last_progress = now
        return state.digest.hexdigest()
    except KeyboardInterrupt:
        primary_error = True
        state.stop_reason = "keyboard_interrupt"
        raise
    except Exception:
        primary_error = True
        state.stop_reason = operation + "_error"
        raise
    finally:
        # Finalization must not hide the primary error or leave the sink open.
        for label, action in (
            ("capture_sync", lambda: sync_sink(capture) if capture is not None else None),
            ("writer_close", lambda: writer.close() if writer is not None else None),
            ("source_timeout_restore", lambda: source.restore_read_timeout()
             if hasattr(source, "restore_read_timeout") else None),
        ):
            try:
                action()
            except Exception as error:
                state.finalization_errors.append(dict(component=label, error=f"{type(error).__name__}: {error}"))
        if writer is not None and writer.segments:
            segment = writer.segments[-1]
            segment["complete"] = state.stop_reason in ("deadline", "source_exhausted") and not state.finalization_errors
            segment["stop_reason"] = state.stop_reason
            if not segment["complete"] and segment["status"] == "closed":
                segment["status"] = "closed_partial"
        if state.finalization_errors and not primary_error:
            state.stop_reason = "finalization_error"
            raise OSError(f"Capture finalization failed: {state.finalization_errors}")

