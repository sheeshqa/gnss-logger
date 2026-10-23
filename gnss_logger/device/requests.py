"""One pending read request. ACK never substitutes for a requested data response."""

from .adapters import detect_adapter
from .ubx import MON_VER_POLL, UBXParser, ack, mon_ver, valget, valget_request


def ensure_read_only(data):
    p = UBXParser()
    frames = p.feed(data)
    if len(frames) != 1 or p.buffer or p.bad_checksums or p.discarded_bytes:
        raise PermissionError("Only one complete read request is permitted")
    cls, msg, payload = frames[0]
    if (cls, msg, payload) == (10, 4, b""):
        return
    if (cls, msg) == (6, 0x8B):
        request = valget(payload)
        if (request["direction"] == "request" and request["layer"] == 0 and
                request["position"] == 0 and 1 <= len(request["keys"]) <= 3 and
                set(request["keys"]) <= {0x30210001, 0x209102A7, 0x20910234}):
            return
    raise PermissionError("Stage 1: packet is outside the read-only allowlist")


class Probe:
    def __init__(self, sender, timeout=2.0, inspect=False):
        self.sender = sender
        self.timeout = timeout
        self.inspect = inspect
        self.pending = None
        self.results = []
        self.adapter = None

    def _send(self, data, expected, now, keys=None):
        ensure_read_only(data)
        self.sender(data)
        self.pending = dict(expected=expected, deadline=now + self.timeout, keys=keys)

    def start(self, now):
        self._send(MON_VER_POLL, (10, 4), now)

    def tick(self, now):
        if self.pending and now >= self.pending["deadline"]:
            self.results.append(dict(request=self.pending["expected"], status="timeout"))
            self.pending = None

    def handle(self, frame, now):
        if not self.pending:
            return
        cls, msg, payload = frame
        if (cls, msg) in ((5, 0), (5, 1)):
            target = ack(payload)
            if (target["cls"], target["msg"]) == self.pending["expected"]:
                self.results.append(dict(request=self.pending["expected"], status="nak" if msg == 0 else "ack"))
                if msg == 0:
                    self.pending = None
            return
        if (cls, msg) != self.pending["expected"]:
            return
        if (cls, msg) == (10, 4):
            if not payload:  # Logged/echoed poll is not a response.
                return
            identity = mon_ver(payload)
            self.adapter = detect_adapter(identity)
            self.results.append(dict(request=(cls, msg), status="response", identity=identity))
            self.pending = None
            if self.inspect and self.adapter:
                keys = list(self.adapter.inspect_configuration().values())
                self._send(valget_request(keys), (6, 0x8B), now, keys)
        else:
            response = valget(payload)
            if (response["direction"] != "response" or response["layer"] != 0 or
                    response["position"] != 0 or
                    not all(f"{key:08X}" in response["values"] for key in self.pending["keys"])):
                return
            self.results.append(dict(request=(cls, msg), status="response", values=response["values"]))
            self.pending = None
