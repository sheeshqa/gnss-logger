"""Owned serial access for application operations; raw transport stays unchanged."""
from .ownership import current_lease, devices
from .transport import open_port as raw_open_port


def usb_identity(port):
    """Read host inventory only; never probe or auto-select another port."""
    from serial.tools.list_ports import comports
    from .ownership import port_key
    matches = [item for item in comports() if port_key(item.device) == port_key(port)]
    if len(matches) > 1:
        raise OSError('Explicit COM port is ambiguous')
    if not matches:
        return None  # Let transport return its usual external/absent-port error.
    item = matches[0]
    return dict(vid=item.vid, pid=item.pid, usb_serial=item.serial_number or None,
                usb_location=item.location or None)


class OwnedConnection:
    def __init__(self, connection, lease, owns_lease):
        object.__setattr__(self, '_connection', connection)
        object.__setattr__(self, '_lease', lease)
        object.__setattr__(self, '_owns_lease', owns_lease)
        object.__setattr__(self, '_released', False)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def close(self):
        if self._released:
            return
        try:
            self._connection.close()
        finally:
            object.__setattr__(self, '_released', True)
            self._lease.detach()
            if self._owns_lease:
                self._lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_device(port, baudrate, *, identity=None, registry=None, resolver=usb_identity,
                opener=raw_open_port):
    """Return an owned connection; use context manager or close explicitly.

    An enclosing operation lease remains reserved through its finalization.
    Injectable resolver/opener allow complete tests without host inventory or COM.
    """
    registry = registry or devices
    lease = current_lease()
    owns_lease = lease is None
    if owns_lease:
        lease = registry.reserve(port, identity)
    try:
        observed = identity if identity is not None else resolver(port)
        lease.attach(port, observed)
        try:
            connection = opener(port, baudrate)
        except BaseException:
            lease.detach()
            raise
        return OwnedConnection(connection, lease, owns_lease)
    except BaseException:
        if owns_lease:
            lease.close()
        raise
