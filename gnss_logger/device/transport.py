"""No receiver configuration commands. Import pySerial only for live access."""

from .ubx import MON_VER_POLL
from .requests import ensure_read_only


def open_port(port, baudrate):
    import serial
    connection = serial.Serial(port=None, baudrate=baudrate, timeout=0.25,
                               write_timeout=1, xonxoff=False, rtscts=False,
                               dsrdtr=False)
    # Set before open; drivers/USB bridges can still transiently toggle lines.
    connection.dtr = False
    connection.rts = False
    connection.port = port
    try:
        connection.open()
    except BaseException:
        connection.close()
        raise
    return connection


def poll_version(connection):
    send_read_only(connection, MON_VER_POLL)


def send_read_only(connection, data):
    ensure_read_only(data)
    if connection.write(data) != len(data):
        raise OSError("Incomplete MON-VER poll write")
