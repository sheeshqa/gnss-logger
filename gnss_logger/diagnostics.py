"""Observation statistics, never configuration claims or phase correction."""

from collections import Counter

from .device.ubx import UBXParser, ack, mon_ver, rawx, sfrbx, valget
from .device.adapters import detect_adapter
from .phase import PhaseTracker


class Diagnostics:
    def __init__(self, expected_hz=1.0):
        self.parser = UBXParser()
        self.expected_hz = expected_hz
        self.phase_tracker = PhaseTracker(expected_hz)
        self.bytes_received = 0
        self.messages = Counter()
        self.decode_errors = Counter()
        self.rawx_count = self.sfrbx_count = 0
        self.identity = None
        self.signals = Counter()
        self.nav_signals = Counter()
        self.signal_quality = {}
        self.code_valid = self.phase_valid = self.half_cycle_invalid = 0
        self.lock_regressions = self.clock_resets = 0
        self.last_epoch = self.first_epoch = None
        self.last_rawx = None
        self.interval_sum = 0.0
        self.interval_count = 0
        self.interval_min = self.interval_max = None
        self.gap_events = self.missing_estimate = self.nonmonotonic_epochs = 0
        self.cno_min = self.cno_max = None
        self.liveness = None
        self.ack_counts = Counter()
        self.logged_requests = Counter()
        self.configuration = {}
        self.config_pages = 0
        self.adapter = None
        self.gaps = []
        self.gaps_omitted = 0
        self.interval_histogram = Counter()
        self.utc_first = self.utc_last = None

    def feed(self, data):
        self.bytes_received += len(data)
        frames = self.parser.feed(data)
        for cls, msg, payload in frames:
            name = f"{cls:02X}-{msg:02X}"
            self.messages[name] += 1
            try:
                if (cls, msg) == (0x0A, 0x04):
                    if not payload:
                        self.logged_requests[name] += 1
                    else:
                        self.identity = mon_ver(payload)
                        self.adapter = detect_adapter(self.identity)
                elif (cls, msg) == (6, 0x8B):
                    config = valget(payload)
                    if config["direction"] == "request":
                        self.logged_requests[name] += 1
                    else:
                        self.config_pages += 1
                        layer = str(config["layer"])
                        self.configuration.setdefault(layer, {}).update(config["values"])
                elif (cls, msg) in ((5, 0), (5, 1)):
                    target = ack(payload)
                    self.ack_counts[f"{'ACK' if msg else 'NAK'}:{target['cls']:02X}-{target['msg']:02X}"] += 1
                elif (cls, msg) == (2, 0x15):
                    self._rawx(rawx(payload))
                elif (cls, msg) == (2, 0x13):
                    nav = sfrbx(payload)
                    self.sfrbx_count += 1
                    self.nav_signals[f"{nav['gnss_id']}:{nav['sig_id']}"] += 1
            except ValueError as error:
                self.decode_errors[f"{name}: {error}"] += 1
        return frames

    def _rawx(self, epoch):
        self.phase_tracker.update(epoch)
        self.lock_regressions = self.phase_tracker.counts["locktime_decrease"]
        self.rawx_count += 1
        current = epoch["week"] * 604800 + epoch["tow_s"]
        if self.first_epoch is None:
            self.first_epoch = dict(week=epoch["week"], tow_s=epoch["tow_s"])
        self.last_rawx = {k: v for k, v in epoch.items() if k != "measurements"}
        from .timebase import rawx_utc
        timestamp = rawx_utc(epoch)
        if timestamp:
            if self.utc_first is None:
                self.utc_first = timestamp.isoformat()
            self.utc_last = timestamp.isoformat()
        if epoch["clock_reset"]:
            self.clock_resets += 1
        if self.last_epoch is not None and not epoch["clock_reset"]:
            delta = current - self.last_epoch
            if delta <= 0:
                self.nonmonotonic_epochs += 1
            else:
                self.interval_sum += delta
                self.interval_count += 1
                self.interval_histogram[f"{delta:.3f}"] += 1
                self.interval_min = delta if self.interval_min is None else min(self.interval_min, delta)
                self.interval_max = delta if self.interval_max is None else max(self.interval_max, delta)
                if delta > 1.5 / self.expected_hz:
                    self.gap_events += 1
                    self.missing_estimate += max(0, round(delta * self.expected_hz) - 1)
                    gap = dict(from_gps_s=self.last_epoch, to_gps_s=current, interval_s=delta,
                               excess_s=delta - 1 / self.expected_hz,
                               end_receiver_derived_utc=timestamp.isoformat() if timestamp else None)
                    if timestamp:
                        from datetime import timedelta
                        gap["start_receiver_derived_utc"] = (timestamp - timedelta(seconds=delta)).isoformat()
                    if len(self.gaps) < 1000:
                        self.gaps.append(gap)
                    else:
                        self.gaps_omitted += 1
        self.last_epoch = current
        for measurement in epoch["measurements"]:
            m = measurement
            self.signals[f"{m['gnss_id']}:{m['sig_id']}"] += 1
            signal = f"{m['gnss_id']}:{m['sig_id']}"
            quality = self.signal_quality.setdefault(signal, dict(
                measurements=0, code_valid=0, phase_valid=0, satellites=set()))
            quality["measurements"] += 1
            quality["code_valid"] += m["code_valid"]
            quality["phase_valid"] += m["phase_valid"]
            quality["satellites"].add(m["sv_id"])
            self.code_valid += m["code_valid"]
            self.phase_valid += m["phase_valid"]
            self.half_cycle_invalid += not m["half_cycle_valid"]
            cno = m["cno_dbhz"]
            self.cno_min = cno if self.cno_min is None else min(self.cno_min, cno)
            self.cno_max = cno if self.cno_max is None else max(self.cno_max, cno)

    def report(self):
        return dict(
            schema_version=1, bytes_received=self.bytes_received,
            phase_quality=self.phase_tracker.report(),
            live_arrival=self.liveness,
            identity=self.identity, adapter=self.adapter.describe() if self.adapter else None,
            configuration_snapshot=self.configuration or None,
            configuration_pages=self.config_pages, logged_requests=dict(self.logged_requests),
            acknowledgements=dict(self.ack_counts),
            messages=dict(self.messages), decode_errors=dict(self.decode_errors),
            checksum_errors=self.parser.bad_checksums,
            discarded_bytes=self.parser.discarded_bytes,
            oversize_headers=self.parser.oversize_headers,
            pending_bytes=len(self.parser.buffer),
            RAWX=dict(status="observed" if self.rawx_count else "not_observed",
                      epochs=self.rawx_count, first_epoch=self.first_epoch, last_epoch=self.last_rawx,
                      first_receiver_derived_utc=self.utc_first, last_receiver_derived_utc=self.utc_last,
                      signals=dict(self.signals), code_valid=self.code_valid,
                      signal_quality={key: {**value, "satellites": sorted(value["satellites"])}
                                      for key, value in self.signal_quality.items()},
                      phase_valid=self.phase_valid, half_cycle_invalid=self.half_cycle_invalid,
                      cno_min_dbhz=self.cno_min, cno_max_dbhz=self.cno_max,
                      clock_resets=self.clock_resets,
                      lock_regressions_possible_slips=self.lock_regressions),
            SFRBX=dict(status="observed" if self.sfrbx_count else "not_observed",
                       messages=self.sfrbx_count, signals=dict(self.nav_signals),
                       ephemerides_completeness="not_checked"),
            timing=dict(expected_hz=self.expected_hz,
                        mean_interval_s=self.interval_sum / self.interval_count if self.interval_count else None,
                        min_interval_s=self.interval_min, max_interval_s=self.interval_max,
                        gap_events=self.gap_events, estimated_missing_epochs=self.missing_estimate,
                        interval_histogram_s=dict(self.interval_histogram),
                        gaps=self.gaps, gaps_omitted=self.gaps_omitted,
                        nonmonotonic_epochs=self.nonmonotonic_epochs),
        )
