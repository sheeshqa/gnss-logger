"""In-process device admission. No discovery, port opens or hardware commands."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from threading import Lock, get_ident


class DeviceBusyError(OSError):
    """An application operation already owns this port or physical USB device."""


def identity_keys(identity):
    if identity is None:
        return set()
    info = asdict(identity) if is_dataclass(identity) else identity
    vid, pid = info.get('vid'), info.get('pid')
    if vid is None or pid is None:
        return set()
    # Reserve both known identifiers, not model or COM as a physical identity.
    keys = set()
    for name in ('usb_serial', 'usb_location'):
        value = info.get(name)
        if value:
            keys.add((name, vid, pid, str(value)))
    return keys


def port_key(port):
    value = str(port).strip().casefold()
    if value.startswith('\\\\.\\'):
        value = value[4:]
    if not value:
        raise ValueError('An explicit port is required')
    return ('port', value)


_active = ContextVar('gnss_device_lease', default=None)


class DeviceRegistry:
    def __init__(self):
        self._lock = Lock()
        self._owners = {}

    def reserve(self, port, identity=None):
        """Atomically acquire aliases. Caller must close/use context manager."""
        lease = DeviceLease(self)
        lease.add(port, identity)
        return lease

    def _add(self, lease, keys):
        with self._lock:
            if lease._closed:
                raise RuntimeError('Device reservation is closed')
            if any(key in self._owners and self._owners[key] is not lease for key in keys):
                raise DeviceBusyError('GNSS device is busy in this application')
            for key in keys:
                self._owners[key] = lease
            lease._keys.update(keys)

    def _release(self, lease):
        with self._lock:
            for key in lease._keys:
                if self._owners.get(key) is lease:
                    del self._owners[key]
            lease._closed = True


class DeviceLease:
    def __init__(self, registry):
        self.registry = registry
        self._keys = set()
        self._closed = False
        self._connection_lock = Lock()
        self._connected = False

    def add(self, port, identity=None):
        self.registry._add(self, {port_key(port)} | identity_keys(identity))

    def attach(self, port, identity=None):
        # Internal borrowing is allowed once, never a second serial open.
        with self._connection_lock:
            if self._connected:
                raise DeviceBusyError('Device reservation already has an open connection')
            self.add(port, identity)
            self._connected = True

    def detach(self):
        with self._connection_lock:
            self._connected = False

    @contextmanager
    def activate(self):
        token = _active.set((self, get_ident()))
        try:
            yield self
        finally:
            _active.reset(token)

    def close(self):
        self.registry._release(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


devices = DeviceRegistry()


def current_lease():
    """Internal operation context, not inherited by unrelated worker threads."""
    current = _active.get()
    return current[0] if current is not None and current[1] == get_ident() else None
