"""Conservative receiver time policy, independent of the host UTC calendar."""
import math
from ..device.ubx import rawx
from ..timebase import rawx_utc


class TimeResolver:
    def __init__(self, expected_hz=1):
        if not math.isfinite(expected_hz) or expected_hz <= 0:
            raise ValueError('Expected rate must be finite and positive')
        self.period = 1.0 / expected_hz
        self.ttl = max(5.0, 3 * self.period)
        # Includes read batching/scheduler latency; epoch cadence has a tighter check.
        self.tolerance = max(2.0, 0.2 * self.period)
        self.cadence_tolerance = min(0.1, 0.1 * self.period)
        self.state, self.generation = 'UNDATED', 0
        self.anchor = self.candidate = None
        self.reason = 'no_anchor'

    def _drop(self, reason):
        self.generation += 1
        self.state, self.reason = 'DISCONTINUITY', reason
        self.anchor = self.candidate = None

    def _consistent(self, before, after):
        delta = after['gps'] - before['gps']
        arrival = (after['ns'] - before['ns']) / 1e9
        cadence = max(1, round(delta / self.period)) * self.period
        return (before['leap'] == after['leap'] and 0 < delta <= self.ttl
                and 0 <= arrival <= self.ttl
                and abs(delta - arrival) <= self.tolerance
                and abs(delta - cadence) <= self.cadence_tolerance)

    def update(self, frame, monotonic_ns):
        """Return routing and a decision for any prior pending candidate range."""
        result = dict(day=None, hold=False, release=False, release_day=None)
        before = self.candidate or self.anchor
        if before and (monotonic_ns < before['ns'] or (monotonic_ns-before['ns'])/1e9 > self.ttl):
            result['release'] = self.candidate is not None
            self._drop('arrival_gap_or_clock_jump')
        if frame[:2] != (2, 0x15):
            result['hold'] = self.candidate is not None
            result['day'] = self.anchor['day'] if self.anchor else None
            return result
        try:
            epoch = rawx(frame[2])
            if (not epoch['leap_seconds_valid'] or epoch['clock_reset']
                    or not 1024 <= epoch['week'] <= 8191
                    or not -64 <= epoch['leap_seconds'] <= 64):
                raise ValueError('Invalid or ambiguous RAWX time flags/range')
            stamp = rawx_utc(epoch)
            current = dict(gps=epoch['week']*604800+epoch['tow_s'],
                leap=epoch['leap_seconds'], utc=stamp.isoformat(), day=stamp.date().isoformat(), ns=monotonic_ns)
        except (ValueError, OverflowError):
            result['release'] |= self.candidate is not None
            self._drop('invalid_rawx_time')
            return result
        previous = self.candidate or self.anchor
        if previous and not self._consistent(previous, current):
            result['release'] |= self.candidate is not None
            leap_changed = previous['leap'] != current['leap']
            self._drop('leap_ambiguity' if leap_changed else 'gnss_discontinuity')
            if leap_changed:
                return result  # Do not date the transition frame itself.
        if self.candidate:
            result.update(release=True, release_day=self.candidate['day'], day=current['day'])
            self.state, self.reason = 'VALID', 'two_consistent_rawx'
            self.anchor, self.candidate = current, None
        elif self.anchor and self.anchor['day'] == current['day']:
            self.anchor = current
            result['day'] = current['day']
        else:
            self.state, self.reason = 'CANDIDATE', 'new_anchor_or_day'
            self.candidate, self.anchor = current, None
            result['hold'] = True
        return result

    def snapshot(self):
        return dict(state=self.state, generation=self.generation, reason=self.reason,
                    anchor=self.anchor, candidate=self.candidate)

    def binding(self):
        return dict(status='VALID' if self.anchor else ('DISCONTINUITY' if self.state == 'DISCONTINUITY' else 'UNDATED'),
            receiver_utc=self.anchor['utc'] if self.anchor else None,
            source='RAWX' if self.anchor else 'NONE', leap_seconds_valid=self.anchor is not None,
            generation=self.generation)

    def policy(self):
        return dict(period_s=self.period, ttl_s=self.ttl, tolerance_s=self.tolerance, anchors=2)
