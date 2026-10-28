"""Canonical source-offset framing. Input blocks are bounded by the caller."""
from ..device.ubx import UBXParser


class Framer:
    def __init__(self, offset=0):
        self.parser = UBXParser(max_payload=65535)
        self.offset = offset

    def feed(self, data):
        for original, frame in self.parser.feed_records(data):
            start = self.offset
            self.offset += len(original)
            yield start, original, frame

    def tail(self):
        original = bytes(self.parser.buffer)
        self.parser.buffer.clear()
        start = self.offset
        self.offset += len(original)
        return start, original
