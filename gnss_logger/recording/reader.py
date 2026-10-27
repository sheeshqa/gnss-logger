"""One physical reader, explicit port/USB binding, only a MON-VER read request."""
from datetime import datetime, timezone
import time

from ..device.transport import open_port, send_read_only
from ..device.ubx import MON_VER_POLL
from .contracts import InputChunk

READ_SIZE = 4096


class SerialReader:
    simulated = False

    def __init__(self, connection, connection_id, usb):
        self.connection = connection
        self.connection_id = connection_id
        self.usb = usb
        self.sequence = self.offset = 0

    def start(self):
        send_read_only(self.connection, MON_VER_POLL)

    def read(self, remaining_s):
        timeout = min(0.25, max(0.0, remaining_s)) if remaining_s is not None else 0.25
        if self.connection.timeout != timeout:
            self.connection.timeout = timeout
        data = self.connection.read(READ_SIZE)
        chunk = InputChunk(self.connection_id, self.sequence, self.offset, data,
                           datetime.now(timezone.utc).isoformat(), time.monotonic_ns())
        if data:
            self.sequence += 1
            self.offset += len(data)
        return chunk

    def close(self):
        self.connection.close()


def make_reader(settings, connection_id):
    from serial.tools.list_ports import comports
    matches = [p for p in comports() if p.device.casefold() == settings.initial_port.casefold()]
    if len(matches) != 1:
        raise OSError('Explicit COM port is absent or ambiguous; no automatic selection/reconnect')
    port = matches[0]
    usb = dict(vid=port.vid, pid=port.pid, usb_serial=port.serial_number or None, usb_location=port.location or None)
    # Model is verified later, from the one incoming stream, after journaling begins.
    if not settings.binding.matches(dict(usb, model=settings.binding.model)):
        raise OSError('COM port USB attributes do not match the explicit receiver binding')
    return SerialReader(open_port(port.device, settings.baud), connection_id, usb)
