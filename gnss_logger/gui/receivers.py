"""Thin receiver API adapters. No port is scanned/opened at import time."""
from datetime import datetime, timezone
from threading import Event
import time
from types import SimpleNamespace

from .. import application as api


class ReceiverBackend:
    is_mock = False

    def devices(self):
        return api.discover_receivers()

    def diagnose(self, request):
        return api.diagnose(request)

    def create_recording(self, settings, *, conversion_settings=None):
        return (api.RecordingHandle(settings, conversion_settings=conversion_settings)
                if conversion_settings is not None else api.RecordingHandle(settings))


class MockReceiverBackend(ReceiverBackend):
    """Exercise the real application services using existing synthetic UBX/DI.

    No pySerial transport is used. One backend owns one shared synthetic registry.
    Mock GNSS time advances 1 second per epoch; delivery is accelerated for UI tests.
    """
    is_mock = True

    def __init__(self, scenario='normal', interval=0.1):
        from ..device.ownership import DeviceRegistry
        self.registry = DeviceRegistry()
        self.scenario = scenario
        self.interval = interval

    def _inventory(self):
        return [SimpleNamespace(device='MOCK-' + suffix, description='SIMULATED ' + suffix,
                                hwid='MOCK', vid=0x1546, pid=0x01A9,
                                serial_number='MOCK-' + suffix, location='mock/' + suffix)
                for suffix in ('X20P', 'F9P')]

    def devices(self):
        return api.discover_receivers(self._inventory())

    def diagnose(self, request):
        from ..mock import MockReceiver
        from ..device.requests import ensure_read_only
        source = MockReceiver('x20p' if request.port == 'MOCK-X20P' else 'f9p')

        class Connection:
            def write(self, data):
                ensure_read_only(data)
                return source.send(data)

            def close(self):
                pass

        return api.diagnose(request, inventory=self._inventory(), registry=self.registry,
                            opener=lambda *args, **kwargs: Connection(),
                            source_factory=lambda connection: source, clock=source.monotonic)

    def create_recording(self, settings, *, conversion_settings=None):
        from ..recording.controller import RecordController
        if not settings.station.startswith('MOCK'):
            raise ValueError('Synthetic recording station must start with MOCK')
        root = getattr(self, 'mock_root', None)
        if root is not None:
            for path in (settings.output, settings.daily_root):
                if path is not None and not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError('MOCK recording directories must stay inside the synthetic workspace')
        controller = RecordController(
            reader_factory=lambda value, connection: MockReader(value, connection,
                                                               self.scenario, self.interval),
            device_registry=self.registry)
        return api.RecordingHandle(settings, controller=controller, conversion_settings=conversion_settings,
                                   worker_factory=getattr(self, 'conversion_worker_factory', None))


class MockReader:
    """A synthetic Reader, not a recorder. The existing controller saves bytes."""
    simulated = True

    def __init__(self, settings, connection_id, scenario='normal', interval=0.1):
        from ..mock import MockReceiver
        profile = 'x20p' if settings.binding.model == 'ZED-X20P' else 'f9p'
        self.source = MockReceiver(profile, epochs=1_000_000)
        self.connection_id = connection_id
        self.usb = dict(vid=settings.binding.vid, pid=settings.binding.pid,
                        usb_serial=settings.binding.usb_serial,
                        usb_location=settings.binding.usb_location)
        self.scenario, self.interval = scenario, interval
        self._iterator = iter(self.source)
        self.sequence = self.offset = self.reads = 0
        self.closed = False

    def start(self):
        from ..device.ubx import MON_VER_POLL
        self.source.send(MON_VER_POLL)

    def read(self, remaining_s):
        from ..recording.contracts import InputChunk
        delay = self.interval if remaining_s is None else min(self.interval, max(0, remaining_s))
        Event().wait(delay)
        self.reads += 1
        if self.scenario == 'disconnect' and self.reads == 4:
            raise OSError('MOCK: receiver disconnected')
        data = next(self._iterator).data
        chunk = InputChunk(self.connection_id, self.sequence, self.offset, data,
                           datetime.now(timezone.utc).isoformat(), time.monotonic_ns())
        if data:
            self.sequence += 1
            self.offset += len(data)
        return chunk

    def close(self):
        self.closed = True
