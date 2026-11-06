"""Streaming structural checks for RINEX 3.02 observation and navigation files."""
from collections import Counter
from datetime import datetime, timedelta
import math
import re

SATELLITE = re.compile(r'[GRECJSI][0-9]{2}')


def number(value):
    parsed = float(value.replace('D','E').replace('d','e'))
    if not math.isfinite(parsed):
        raise ValueError('Nonfinite RINEX value')
    return parsed


def timestamp(fields):
    second = number(fields[5])
    if not 0 <= second < 61:
        raise ValueError('Invalid RINEX seconds')
    return datetime(*map(int,fields[:5])) + timedelta(seconds=second)


def header(stream, kind):
    line = stream.readline()
    if line[:9].strip()!='3.02' or line[20:21]!=kind or line[60:].strip()!='RINEX VERSION / TYPE':
        raise ValueError('Expected RINEX 3.02 '+kind+' header')
    system = line[40:41].strip()
    types, declared, current, timescale = {}, {}, None, None
    for _ in range(10000):
        line = stream.readline()
        if not line:
            break
        label = line[60:].strip()
        if label=='END OF HEADER':
            if any(len(types[k])!=n for k,n in declared.items()):
                raise ValueError('Incomplete OBS types header')
            return system,types,timescale
        if label=='TIME OF FIRST OBS':
            timescale = line[48:51].strip() or None
        if label=='SYS / # / OBS TYPES':
            if line[0]!=' ':
                current=line[0]
                declared[current]=int(line[3:6])
                types[current]=[]
            if current is None:
                raise ValueError('Invalid OBS types continuation')
            types[current].extend(line[7:60].split())
    raise ValueError('Missing END OF HEADER')


def observations(path, interval):
    with path.open(encoding='ascii') as stream:
        system, types, timescale = header(stream,'O')
        counts, signals, satellites = Counter(), set(), set()
        first=previous=None
        epochs=events=gaps=nonmonotonic=0
        examples=[]
        for line in stream:
            if not line.strip():
                continue
            if not line.startswith('>'):
                raise ValueError('Expected OBS epoch')
            fields=line[1:].split()
            if len(fields)<8:
                raise ValueError('Truncated OBS epoch')
            current=timestamp(fields)
            flag, count=map(int,fields[6:8])
            if not 0<=flag<=6 or not 0<=count<=999:
                raise ValueError('Invalid OBS epoch flag or record count')
            actual=0
            for _ in range(count):
                row=stream.readline().rstrip('\r\n')
                if not row or row.startswith('>'):
                    raise ValueError('Truncated OBS epoch records')
                if flag not in (0,1):
                    continue
                sat=row[:3]
                if not SATELLITE.fullmatch(sat) or sat[0] not in types:
                    raise ValueError('Invalid OBS satellite record')
                present=False
                for index, code in enumerate(types[sat[0]]):
                    field=row[3+16*index:17+16*index].strip()
                    if field:
                        number(field)
                        signals.add(sat[0]+code)
                        present=True
                if present:
                    counts[sat[0]]+=1
                    satellites.add(sat)
                    actual+=1
            if flag not in (0,1):
                events+=1
            elif actual:
                epochs+=1
                first=first or current
                if previous is not None:
                    delta=(current-previous).total_seconds()
                    if delta<=0:
                        nonmonotonic+=1
                    elif delta>interval*1.1:
                        gaps+=1
                        if len(examples)<20:
                            examples.append(dict(before=previous.isoformat(),after=current.isoformat(),seconds=delta))
                previous=current
        if not epochs:
            raise ValueError('OBS has no epochs with numeric observations')
        return dict(version='3.02',type='OBS',system=system,epochs=epochs,events=events,
                    satellite_observations=dict(counts),unique_satellites=len(satellites),
                    signals=sorted(signals),time_system=timescale,first_epoch=first.isoformat(),
                    last_epoch=previous.isoformat(),gaps=gaps,gap_examples=examples,
                    nonmonotonic_epochs=nonmonotonic)


def navigation(path):
    with path.open(encoding='ascii') as stream:
        system, _, _ = header(stream,'N')
        counts=Counter()
        for line in stream:
            if not line.strip():
                continue
            sat=line[:3]
            if not SATELLITE.fullmatch(sat):
                raise ValueError('Invalid NAV satellite record')
            fields=line[3:23].split()
            if len(fields)!=6:
                raise ValueError('Invalid NAV epoch')
            timestamp(fields)
            for offset in (23,42,61):
                number(line[offset:offset+19].strip())
            for _ in range(3 if sat[0] in 'RS' else 7):
                row=stream.readline()
                if not row.startswith('    '):
                    raise ValueError('Truncated NAV record')
                values=[row[i:i+19].strip() for i in (4,23,42,61)]
                if not values[0]:
                    raise ValueError('NAV continuation has no numeric data')
                for value in filter(None,values):
                    number(value)
            counts[sat[0]]+=1
        if not counts:
            raise ValueError('NAV has no navigation records')
        if system!='M' and set(counts)!={system}:
            raise ValueError('NAV header does not identify its constellation(s)')
        return dict(version='3.02',type='NAV',system=system,records=dict(counts))
