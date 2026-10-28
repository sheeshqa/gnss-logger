"""Atomic NTFS/local-filesystem name claims. Never append to an old artifact."""
from pathlib import Path
import re


def station_component(station):
    if (not isinstance(station, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', station)
            or station.upper() in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
            or re.fullmatch(r'(COM|LPT)[1-9]', station.upper())):
        raise ValueError('Station must be a safe Windows directory component')
    return station


def reserve(root, station, day):
    """Return exclusively opened part, final path; part is also the shared claim."""
    station_component(station)
    root = Path(root)
    if day is None:
        directory = root / station / 'undated'
        base, number = station + '_undated', 1
    else:
        from datetime import date
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('Canonical UTC date required')
        directory = root / station / day[:4] / day[5:7]
        base, number = station + '_' + day.replace('-', ''), 0
    directory.mkdir(parents=True, exist_ok=True)
    # Re-scan under the atomic claim: checking exists alone is never a lock.
    while True:
        suffix = '' if number == 0 else '_{:03d}'.format(number)
        final = directory / (base + suffix + '.ubx')
        part = final.with_name(final.name + '.part')
        number += 1
        if final.exists():
            continue
        try:
            handle = part.open('xb', buffering=0)
        except FileExistsError:
            continue
        if final.exists():
            handle.close()
            part.unlink()  # Only this process's new empty claim; no observation written.
            continue
        return handle, part, final
