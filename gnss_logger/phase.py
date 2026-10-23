"""Phase continuity indicators only; never repairs measurements or proves cycle slips."""

from collections import Counter
from datetime import timedelta

from .timebase import rawx_utc


class PhaseTracker:
    def __init__(self, expected_hz=1, limit=1000):
        self.period = 1 / expected_hz
        self.limit = limit
        self.channels = {}
        self.counts = Counter()
        self.per_channel = {}
        self.events = []
        self.omitted = 0
        self.last_epoch = None
        self.measurements = self.invalid_phase = self.invalid_half = 0

    def emit(self, kind, key, timestamp, **fields):
        self.counts[kind] += 1
        label = ":".join(map(str, key)) if key is not None else "receiver"
        self.per_channel.setdefault(label, Counter())[kind] += 1
        event = dict(kind=kind, channel=label, receiver_derived_utc=timestamp, **fields)
        if len(self.events) < self.limit:
            self.events.append(event)
        else:
            self.omitted += 1

    def update(self, epoch):
        now = epoch["week"] * 604800 + epoch["tow_s"]
        utc = rawx_utc(epoch)
        stamp = utc.isoformat() if utc else None
        if epoch["clock_reset"] or (self.last_epoch is not None and now <= self.last_epoch):
            self.emit("receiver_clock_reset" if epoch["clock_reset"] else "epoch_time_discontinuity",
                      None, stamp)
            self.channels.clear()
        seen = set()
        for m in epoch["measurements"]:
            key = (m["gnss_id"], m["sv_id"], m["sig_id"], m["freq_id"])
            seen.add(key)
            old = self.channels.get(key)
            self.measurements += 1
            self.invalid_phase += not m["phase_valid"]
            self.invalid_half += not m["half_cycle_valid"]
            if old is not None:
                delta = now - old["time"]
                gap = delta > 1.5 * self.period
                if gap:
                    self.emit("observation_gap", key, stamp, interval_s=delta,
                              excess_s=delta - self.period,
                              start_receiver_derived_utc=old["utc"])
                if old["phase_valid"] and not m["phase_valid"]:
                    self.emit("phase_validity_lost", key, stamp)
                if m["phase_valid"] and (not old["phase_valid"] or gap):
                    self.emit("phase_reacquired_indicator", key, stamp,
                              reason="observation_gap" if gap else "phase_validity_returned")
                if old["half_valid"] != m["half_cycle_valid"]:
                    self.emit("half_cycle_validity_changed", key, stamp,
                              was_valid=old["half_valid"], is_valid=m["half_cycle_valid"])
                if old["sub_half"] != m["half_cycle_subtracted"]:
                    self.emit("half_cycle_adjustment_flag_changed", key, stamp)
                # Keep last VALID lock through invalid/missing observations (N6).
                previous_lock = old["last_valid_lock"]
                if m["phase_valid"] and previous_lock is not None and m["lock_ms"] < previous_lock:
                    self.emit("locktime_decrease", key, stamp, previous_ms=previous_lock,
                              current_ms=m["lock_ms"], after_gap=gap,
                              after_invalid_phase=not old["phase_valid"])
            elif not m["phase_valid"]:
                self.emit("initial_phase_invalid", key, stamp)
            self.channels[key] = dict(time=now, utc=stamp, phase_valid=m["phase_valid"],
                half_valid=m["half_cycle_valid"], sub_half=m["half_cycle_subtracted"], missing=False,
                last_valid_lock=m["lock_ms"] if m["phase_valid"] else old["last_valid_lock"] if old else None)
        for key, old in self.channels.items():
            if key not in seen and not old["missing"]:
                self.emit("channel_missing", key, stamp, last_seen_receiver_derived_utc=old["utc"],
                          interval_since_last_seen_s=now - old["time"])
                old["missing"] = True
        self.last_epoch = now

    def report(self):
        warning = self.invalid_phase or self.invalid_half or bool(self.counts)
        return dict(status="NO_DATA" if not self.measurements else "WARNINGS" if warning else "NO_INDICATORS_DETECTED",
                    interpretation="Tracking indicators are not proven cycle slips; no scientific acceptance is implied",
                    measurements=self.measurements, invalid_phase_measurements=self.invalid_phase,
                    half_cycle_invalid_measurements=self.invalid_half, counts=dict(self.counts),
                    channel_key="gnssId:svId:sigId:freqId",
                    per_channel={key: dict(value) for key, value in self.per_channel.items()},
                    events=list(self.events), events_omitted=self.omitted,
                    event_limit=self.limit)

