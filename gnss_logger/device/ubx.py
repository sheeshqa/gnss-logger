"""Clean-room UBX framing; bounded memory, checksum before decoding."""

import struct


def checksum(data):
    a = b = 0
    for value in data:
        a = (a + value) & 255
        b = (b + a) & 255
    return bytes((a, b))


# Only outgoing command in this implementation: zero-payload MON-VER poll.
MON_VER_POLL = bytes.fromhex("b5 62 0a 04 00 00 0e 34")


def packet(cls, msg, payload=b""):
    """Build a packet, never send it. Sending has a separate read-only guard."""
    if not 0 <= cls <= 255 or not 0 <= msg <= 255 or len(payload) > 65535:
        raise ValueError("Invalid UBX packet header")
    body = bytes((cls, msg)) + struct.pack("<H", len(payload)) + payload
    return b"\xb5\x62" + body + checksum(body)


def value_size(key):
    try:
        return {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}[(key >> 28) & 7]
    except KeyError:
        raise ValueError(f"Unknown configuration storage size: {key:08X}") from None


def valget_request(keys, layer=0, position=0):
    if not 1 <= len(keys) <= 64 or layer not in (0, 1, 2, 7) or not 0 <= position <= 65535:
        raise ValueError("Invalid VALGET request")
    return packet(6, 0x8B, struct.pack("<BBH", 0, layer, position) +
                  b"".join(struct.pack("<I", k) for k in keys))


def valget(payload):
    if len(payload) < 4 or payload[0] not in (0, 1) or payload[1] not in (0, 1, 2, 7):
        raise ValueError("Invalid VALGET header")
    version, layer, position = struct.unpack_from("<BBH", payload)
    if version == 0:
        if (len(payload) - 4) % 4:
            raise ValueError("Invalid VALGET key list")
        return dict(direction="request", layer=layer, position=position,
                    keys=[k[0] for k in struct.iter_unpack("<I", payload[4:])])
    values = {}
    offset = 4
    while offset < len(payload):
        if offset + 4 > len(payload):
            raise ValueError("Truncated VALGET key")
        key = struct.unpack_from("<I", payload, offset)[0]
        size = value_size(key)
        offset += 4
        if offset + size > len(payload):
            raise ValueError("Truncated VALGET value")
        values[f"{key:08X}"] = int.from_bytes(payload[offset:offset + size], "little")
        offset += size
    return dict(direction="response", layer=layer, position=position, values=values)


def ack(payload):
    if len(payload) != 2:
        raise ValueError("Invalid ACK/NAK length")
    return dict(cls=payload[0], msg=payload[1])


class UBXParser:
    def __init__(self, max_payload=16384):
        if type(max_payload) is not int or not 0 <= max_payload <= 65535:
            raise ValueError('Invalid UBX framing payload limit')
        self.max_payload = max_payload
        self.buffer = bytearray()
        self.bad_checksums = 0
        self.discarded_bytes = 0
        self.oversize_headers = 0

    def feed(self, data):
        return [frame for _, frame in self.feed_records(data) if frame is not None]

    def feed_records(self, data):
        """Lossless tokens: (original bytes, validated frame or None for junk)."""
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            pos = self.buffer.find(b"\xb5\x62")
            if pos < 0:
                keep = int(self.buffer[-1] == 0xB5)
                if len(self.buffer) - keep:
                    frames.append((bytes(self.buffer[:len(self.buffer) - keep]), None))
                self.discarded_bytes += len(self.buffer) - keep
                self.buffer[:] = self.buffer[-1:] if keep else b""
                break
            if pos:
                frames.append((bytes(self.buffer[:pos]), None))
                self.discarded_bytes += pos
                del self.buffer[:pos]
            if len(self.buffer) < 6:
                break
            length = int.from_bytes(self.buffer[4:6], "little")
            # Diagnostic subset, not a universal UBX decoder. RAWX <= 8176.
            if length > self.max_payload:
                self.oversize_headers += 1
                self.discarded_bytes += 1
                frames.append((bytes(self.buffer[:1]), None))
                del self.buffer[0]
                continue
            total = length + 8
            if len(self.buffer) < total:
                break
            if checksum(self.buffer[2:total - 2]) != self.buffer[total - 2:total]:
                self.bad_checksums += 1
                self.discarded_bytes += 1
                frames.append((bytes(self.buffer[:1]), None))
                del self.buffer[0]
                continue
            frames.append((bytes(self.buffer[:total]),
                           (self.buffer[2], self.buffer[3], bytes(self.buffer[6:total - 2]))))
            del self.buffer[:total]
        return frames


def mon_ver(payload):
    if len(payload) < 40 or (len(payload) - 40) % 30:
        raise ValueError("Invalid MON-VER length")
    def string(data):
        return data.split(b"\0", 1)[0].decode("ascii", errors="replace")
    extensions = [string(payload[i:i + 30]) for i in range(40, len(payload), 30)]
    fields = dict(item.split("=", 1) for item in extensions if "=" in item)
    model = fields.get("MOD")
    return {
        "software": string(payload[:30]), "hardware": string(payload[30:40]),
        "extensions": extensions, "model": model,
        "firmware": fields.get("FWVER"), "protocol": fields.get("PROTVER"),
        "adapter_status": "read-only adapter available; hardware/FW unvalidated"
        if model in ("ZED-F9P", "ZED-X20P") else "unverified model",
    }


def rawx(payload):
    if len(payload) < 16:
        raise ValueError("Short RAWX")
    tow, week, leap, count, status, version = struct.unpack_from("<dHbBBB", payload)
    if version != 1 or len(payload) != 16 + count * 32:
        raise ValueError("Unsupported RAWX version or inconsistent numMeas")
    import math
    if not math.isfinite(tow) or not 0 <= tow < 604800:
        raise ValueError("Invalid RAWX time of week")
    measurements = []
    for offset in range(16, len(payload), 32):
        values = struct.unpack_from("<ddfBBBBHBBBBBB", payload, offset)
        pr, cp, doppler, gnss, sv, sig, freq, lock, cno, prs, cps, dos, track, _ = values
        measurements.append(dict(
            pseudorange_m=pr, carrier_cycles=cp, doppler_hz=doppler,
            gnss_id=gnss, sv_id=sv, sig_id=sig, freq_id=freq,
            lock_ms=lock, cno_dbhz=cno, pr_stdev_raw=prs,
            cp_stdev_raw=cps, do_stdev_raw=dos, tracking_status=track,
            code_valid=bool(track & 1) and math.isfinite(pr),
            phase_valid=bool(track & 2) and (cps & 15) != 15 and math.isfinite(cp),
            half_cycle_valid=bool(track & 4), half_cycle_subtracted=bool(track & 8),
        ))
    return dict(tow_s=tow, week=week, leap_seconds=leap,
                leap_seconds_valid=bool(status & 1), clock_reset=bool(status & 2),
                measurements=measurements)


def sfrbx(payload):
    if len(payload) < 8 or payload[6] != 2 or len(payload) != 8 + 4 * payload[4]:
        raise ValueError("Unsupported SFRBX version or inconsistent numWords")
    if not 1 <= payload[4] <= 16:
        raise ValueError("Invalid SFRBX word count")
    return dict(gnss_id=payload[0], sv_id=payload[1], sig_id=payload[2],
                freq_id=payload[3], num_words=payload[4])
