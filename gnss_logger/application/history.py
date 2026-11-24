"""Read recorded metadata without repairing, hashing, or publishing any files.

Integrity values describe past recording decisions. Presence is a separate current
observation; neither is an independent verification of file contents.
"""
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import re
from threading import RLock

from ..recording.schema import validate_record


def _text(value):
    return isinstance(value, str) and bool(value)


def _fingerprint(value):
    size = value.get('bytes', value.get('size'))
    sha = value.get('sha256')
    if type(size) is int and size >= 0 and isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha):
        return sha, size
    return None


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _check_fingerprint(value):
    for key in ('bytes', 'size'):
        if value.get(key) is not None and (type(value[key]) is not int or value[key] < 0):
            raise ValueError('Invalid fingerprint byte count')
    sha = value.get('sha256')
    if sha is not None and (not isinstance(sha, str) or re.fullmatch('[0-9a-f]{64}', sha) is None):
        raise ValueError('Invalid fingerprint SHA-256')


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(name + ' must be an object')
    return value


def _rows(value, name):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(name + ' must be an array of objects')
    return value


def _path(raw, base):
    if not _text(raw):
        raise ValueError('Invalid metadata path')
    path = Path(raw)
    return (path if path.is_absolute() else base / path).resolve()


def _stage1(value, report=False):
    versions = (1, 2) if report else (1,)
    if ('schema_id' in value or type(value.get('schema_version')) is not int
            or value['schema_version'] not in versions or not _text(value.get('session_id'))):
        raise ValueError('Unsupported Stage1 metadata')
    for row in _rows(value.get('artifacts', []) if report else value.get('artifacts'), 'artifacts'):
        if not _text(row.get('path')):
            raise ValueError('Artifact path required')
        _check_fingerprint(row)
    for key in ('identity', 'adapter', 'parameters', 'paths', 'run'):
        if value.get(key) is not None:
            _object(value[key], key)
    for key in ('started_utc', 'ended_utc', 'stop_reason'):
        if value.get(key) is not None and not _text(value[key]):
            raise ValueError('Invalid ' + key)
    if value.get('exit_code') is not None and type(value['exit_code']) is not int:
        raise ValueError('Invalid exit_code')
    registration = value.get('registration')
    if registration is not None and not isinstance(registration, (str, dict)):
        raise ValueError('Invalid registration')
    if isinstance(registration, dict) and not _text(registration.get('status')):
        raise ValueError('Registration status required')
    for key in ('errors', 'warnings'):
        if key in value and (not isinstance(value[key], list)
                            or any(not isinstance(item, (str, dict)) for item in value[key])):
            raise ValueError('Invalid ' + key)
    if 'elapsed_s' in value and not _finite_number(value['elapsed_s']):
        raise ValueError('Invalid elapsed_s')


class HistoryService:
    """A fresh metadata snapshot per query; returned dictionaries are detached.

    Session keys are normalized metadata-directory paths, avoiding collisions
    between copied runs. ``diagnostics`` contains discovery failures from the
    latest query, including entries too malformed to have a session identity.
    Conversion entries distinguish operations from receipts: receipts are commit
    evidence, not additional conversion runs. No relationship is inferred from
    a shared folder or filename.
    """

    def __init__(self, recording_roots, conversion_roots, stage1_roots=()):
        self.recording_roots = tuple(Path(p).resolve() for p in recording_roots)
        self.conversion_roots = tuple(Path(p).resolve() for p in conversion_roots)
        self.stage1_roots = tuple(Path(p).resolve() for p in stage1_roots)
        self._lock = RLock()
        self._diagnostics = []

    @property
    def diagnostics(self):
        """Detached discovery diagnostics from the latest completed query."""
        with self._lock:
            return deepcopy(self._diagnostics)

    def _error(self, path, error, diagnostics=None):
        row = dict(path=str(path), message=f'{type(error).__name__}: {error}')
        self._diagnostics.append(row)
        if diagnostics is not None:
            diagnostics.append(row)

    def _read(self, path):
        value = json.loads(path.read_text(encoding='utf-8-sig'),
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
        return _object(value, 'metadata')

    def _discover(self, roots, names):
        found = set()
        for root in roots:
            try:
                if not root.is_dir():
                    raise ValueError('Metadata root is absent or not a directory')
                for name in names:
                    for path in root.rglob(name):
                        resolved = path.resolve()
                        if path.is_symlink() or not resolved.is_relative_to(root):
                            self._error(path, ValueError('Metadata escapes configured root'))
                            continue
                        found.add(resolved)
            except (OSError, ValueError, RuntimeError) as error:
                self._error(root, error)
        return sorted(found)

    def _file(self, raw, base):
        path = _path(raw, base)
        return dict(path=str(path), present=path.is_file(), independently_verified=False)

    def _artifact(self, row, directory, provisional=False):
        return dict(metadata=row, provisional=provisional,
                    state=row.get('state', row.get('status')), kind=row.get('kind'),
                    bytes=row.get('bytes'), sha256=row.get('sha256'),
                    artifact_id=row.get('artifact_id'), utc_day=row.get('utc_day'),
                    **self._file(row['path'], directory))

    def _session(self, directory, stage1):
        errors = []
        result = dict(session_key=str(directory), stage=1 if stage1 else 2,
                      diagnostics=errors, artifacts=[], manifest=None, report=None,
                      terminal=False, state='INCOMPLETE', recorded_integrity=None,
                      independently_verified=False, conversions=[])
        terminal = directory / 'manifest.json'
        for name in ('report.json', 'manifest.start.json', 'manifest.json'):
            path = directory / name
            if not path.exists():
                continue
            try:
                if path.is_symlink():
                    raise ValueError('Symlink metadata is not followed')
                value = self._read(path)
                if name == 'report.json':
                    if stage1:
                        _stage1(value, report=True)
                    else:
                        if value.get('schema_id') != 'gnss_logger.stage2.diagnostic':
                            raise ValueError('Expected Stage2 diagnostic report')
                        validate_record(value)
                        if (value['run_id'] != directory.parents[1].name
                                or value['attempt_id'] != directory.name):
                            raise ValueError('Report directory ownership conflict')
                    result['report'] = value
                    continue
                if stage1:
                    _stage1(value)
                    if not _text(value.get('ended_utc')) or type(value.get('exit_code')) is not int:
                        raise ValueError('Stage1 terminal fields required')
                else:
                    if value.get('schema_id') != 'gnss_logger.stage2.manifest':
                        raise ValueError('Expected Stage2 manifest')
                    validate_record(value)
                    if (value['run_id'] != directory.parents[1].name
                            or value['attempt_id'] != directory.name):
                        raise ValueError('Manifest directory ownership conflict')
                result['manifest'] = value
                result['terminal'] = path == terminal and (stage1 or value['state'] == 'STOPPED')
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
                self._error(path, error, errors)
        manifest = result['manifest']
        report = result['report']
        if manifest and report and any(manifest.get(k) != report.get(k) for k in
                                       (('session_id',) if stage1 else ('run_id', 'attempt_id'))):
            self._error(directory / 'report.json', ValueError('Report and manifest ownership conflict'), errors)
            report = result['report'] = None
        if manifest is None:
            errors.append(dict(path=str(terminal), message='No valid manifest; completion is unknown'))
            self._summary(result, report or {}, report or {}, directory, provisional=True)
            for row in (report or {}).get('artifacts', []):
                try:
                    result['artifacts'].append(self._artifact(row, directory, provisional=True))
                except (OSError, ValueError, RuntimeError) as error:
                    self._error(directory, error, errors)
            result['ubx_files'] = deepcopy(result['artifacts'])
            return result
        for key in ('session_id', 'run_id', 'attempt_id', 'started_utc', 'ended_utc', 'exit_code', 'stop_reason'):
            result[key] = manifest.get(key)
        self._summary(result, manifest, report or {}, directory, provisional=False)
        result['state'] = (result['registration'] or 'STOPPED' if stage1 else manifest['state']) if result['terminal'] else 'INCOMPLETE'
        result['recorded_integrity'] = manifest.get('integrity')
        rows = list(manifest['artifacts'])
        if not stage1 and manifest['schema_version'] == 2:
            try:
                artifact_dir = _path(manifest['projection']['artifact_directory'], directory)
                if not artifact_dir.is_relative_to(directory):
                    raise ValueError('Projection metadata directory escapes recording attempt')
                if not artifact_dir.is_dir():
                    raise ValueError('Projection metadata directory is absent')
                for path in sorted(artifact_dir.glob('*.json')):
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(directory):
                            raise ValueError('Projection metadata escapes recording attempt')
                        row = validate_record(self._read(path))
                        if (row['schema_id'] != 'gnss_logger.stage2.artifact'
                                or any(row[k] != manifest[k] for k in ('run_id', 'attempt_id'))
                                or not any(a['kind'] == 'PRIMARY' and a['artifact_id'] == row['journal_id']
                                           and a['connection_id'] == row['connection_id'] for a in manifest['artifacts'])):
                            raise ValueError('Projection artifact ownership conflict')
                        # A terminal artifact supersedes its initial OPEN snapshot.
                        if path.name.endswith('.open.json') and (artifact_dir / (row['artifact_id'] + '.json')).exists():
                            continue
                        rows.append(row)
                    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
                        self._error(path, error, errors)
            except (OSError, ValueError, RuntimeError) as error:
                self._error(directory, error, errors)
        for row in rows:
            try:
                result['artifacts'].append(self._artifact(row, directory))
            except (OSError, ValueError, RuntimeError) as error:
                self._error(directory, error, errors)
        result['ubx_files'] = deepcopy(result['artifacts'])
        return result

    def _summary(self, result, metadata, report, directory, provisional):
        result.update(provisional=provisional, model=None, duration_s=None,
                      registration=None, warnings=deepcopy(report.get('warnings', [])),
                      errors=deepcopy(metadata.get('errors', report.get('errors', []))),
                      paths=dict(directory=str(directory), start=str(directory / 'manifest.start.json'),
                                 report=str(directory / 'report.json'), manifest=str(directory / 'manifest.json')))
        for key in ('session_id', 'run_id', 'attempt_id', 'started_utc', 'ended_utc', 'stop_reason', 'exit_code'):
            result[key] = metadata.get(key)
        registration = metadata.get('registration', report.get('registration'))
        result['registration'] = registration.get('status') if isinstance(registration, dict) else registration
        if result['stage'] == 1:
            adapter = report.get('adapter') or {}
            identity = report.get('identity') or {}
            model = adapter.get('model', identity.get('model'))
            result['identity'] = deepcopy(identity)
        else:
            connections = metadata.get('connections', [])
            models = {c['observed_model'] for c in connections if c.get('observed_model')}
            model = next(iter(models)) if len(models) == 1 else None
            result['identity'] = deepcopy(connections)
        result['model'] = model if _text(model) else None
        duration = report.get('elapsed_s')
        if duration is None and type(metadata.get('elapsed_monotonic_ns')) is int:
            try:
                duration = metadata['elapsed_monotonic_ns'] / 1_000_000_000
            except OverflowError:
                self._error(directory, ValueError('Duration exceeds supported numeric range'), result['diagnostics'])
        if duration is None and metadata.get('started_utc') and metadata.get('ended_utc'):
            try:
                duration = (datetime.fromisoformat(metadata['ended_utc']) - datetime.fromisoformat(metadata['started_utc'])).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
        if _finite_number(duration):
            result['duration_s'] = duration

    def _sessions(self):
        sessions = []
        for roots, stage1 in ((self.recording_roots, False), (self.stage1_roots, True)):
            paths = self._discover(roots, ('manifest.json', 'manifest.start.json', 'report.json'))
            directories = {p.parent for p in paths if stage1 or p.parent.parent.name == 'attempts'}
            sessions.extend(self._session(directory, stage1) for directory in sorted(directories))
        return sessions

    def _conversion(self, path):
        value = self._read(path)
        receipt = value.get('schema_id') == 'gnss_logger.conversion.result'
        expected = 1 if receipt else 2
        if (value.get('schema_id') not in ('gnss_logger.conversion', 'gnss_logger.conversion.result')
                or type(value.get('schema_version')) is not int or value['schema_version'] != expected
                or not _text(value.get('status'))):
            raise ValueError('Unsupported conversion schema or status')
        if receipt:
            identity = _object(value.get('identity'), 'identity')
            if _fingerprint(dict(sha256=identity.get('source_sha256'), bytes=identity.get('source_bytes'))) is None:
                raise ValueError('Receipt source fingerprint required')
        elif not _text(value.get('operation_id')):
            raise ValueError('Operation identifier required')
        segments = [value] if receipt else _rows(value.get('segments'), 'segments')
        normalized = []
        for segment in segments:
            source = _object(segment.get('source'), 'source')
            _check_fingerprint(source)
            if not _text(segment.get('status')) or not _text(source.get('path')):
                raise ValueError('Segment status and source path required')
            for key in ('run_id', 'attempt_id', 'artifact_id'):
                if source.get(key) is not None and not _text(source[key]):
                    raise ValueError('Invalid source ownership identifier')
            for key in ('outputs', 'published_outputs', 'output_fingerprints', 'validation'):
                if key in segment:
                    _object(segment[key], key)
            for raw in segment.get('outputs', {}).values():
                _path(raw, path.parent)
            for fingerprint in segment.get('output_fingerprints', {}).values():
                if _fingerprint(_object(fingerprint, 'output fingerprint')) is None:
                    raise ValueError('Invalid output fingerprint')
            for validation in segment.get('validation', {}).values():
                _object(validation, 'output validation')
            for key in ('errors', 'warnings', 'missing_outputs'):
                if key in segment and (not isinstance(segment[key], list)
                                       or any(not isinstance(item, str) for item in segment[key])):
                    raise ValueError(key + ' must be an array of strings')
            outputs = {kind: self._file(raw, path.parent)
                       for kind, raw in segment.get('published_outputs', {}).items()}
            normalized.append(dict(status=segment['status'], source=source,
                                   source_file=self._file(source['path'], path.parent),
                                   published_outputs=outputs, raw=segment,
                                   session_key=None, match='UNLINKED', candidates=[]))
        return dict(path=str(path), kind='receipt' if receipt else 'operation',
                    operation_id=value.get('operation_id'), status=value['status'],
                    segments=normalized, raw=value, independently_verified=False)

    def _conversions(self):
        conversions = []
        # Receipts have source-derived names: inspect JSON metadata, never UBX names.
        for path in self._discover(self.conversion_roots, ('*.json',)):
            try:
                conversions.append(self._conversion(path))
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
                self._error(path, error)
        return conversions

    def _snapshot(self):
        self._diagnostics = []
        sessions, conversions = self._sessions(), self._conversions()
        for conversion in conversions:
            for segment in conversion['segments']:
                source = segment['source']
                candidates, conflicts, fingerprint_conflicts = set(), set(), set()
                identified = any(source.get(k) is not None for k in ('run_id', 'attempt_id', 'artifact_id'))
                for session in sessions:
                    for artifact in session['artifacts']:
                        row = artifact['metadata']
                        owners = {k: row.get(k, session.get(k)) for k in ('run_id', 'attempt_id', 'artifact_id')}
                        contradiction = any(source.get(k) is not None and owners[k] is not None
                                            and source[k] != owners[k] for k in owners)
                        fingerprint = _fingerprint(source)
                        same_hash = fingerprint is not None and fingerprint == _fingerprint(row)
                        same_ids = all(source.get(k) is not None and source[k] == owners[k] for k in owners)
                        if same_ids and fingerprint is not None and _fingerprint(row) is not None and not same_hash:
                            fingerprint_conflicts.add(session['session_key'])
                            continue
                        if contradiction:
                            if same_hash or any(source.get(k) is not None and source[k] == owners[k] for k in owners):
                                conflicts.add(session['session_key'])
                            continue
                        if same_ids or (same_hash and (not identified or all(
                                source.get(k) is None or source[k] == owners[k] for k in owners))):
                            candidates.add(session['session_key'])
                segment['candidates'] = sorted(candidates)
                if fingerprint_conflicts:
                    segment['match'] = 'FINGERPRINT_CONFLICT'
                    segment['candidates'] = sorted(fingerprint_conflicts | candidates)
                elif len(candidates) == 1:
                    segment.update(session_key=next(iter(candidates)), match='MATCHED')
                elif candidates:
                    segment['match'] = 'AMBIGUOUS'
                elif conflicts:
                    segment['match'] = 'OWNERSHIP_CONFLICT'
                    segment['candidates'] = sorted(conflicts)
                for session in sessions:
                    if session['session_key'] == segment['session_key'] and conversion not in session['conversions']:
                        session['conversions'].append(conversion)
        return sessions, conversions

    def list_sessions(self):
        with self._lock:
            return deepcopy(self._snapshot()[0])

    def get_session(self, session_key):
        """Return an exact catalog key; bare IDs are accepted only when unique."""
        with self._lock:
            sessions = self._snapshot()[0]
            matches = [s for s in sessions if str(session_key) in
                       (s['session_key'], s.get('session_id'), s.get('attempt_id'), s.get('run_id'))]
            if len(matches) > 1:
                raise ValueError('Ambiguous session identifier; use session_key')
            return deepcopy(matches[0]) if matches else None

    def list_conversions(self, session_key=None):
        with self._lock:
            conversions = self._snapshot()[1]
            if session_key is not None:
                conversions = [c for c in conversions if any(
                    s['session_key'] == str(session_key) for s in c['segments'])]
            return deepcopy(conversions)
