"""Versioned GUI preferences and bounded, cooperative atomic file writes.

Stores are intended for one worker at a time. Separate instances coordinate using
an exclusive sibling .lock file. Stale locks are reported, never stolen.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from ..conversion.config import default_root
from ..recording.contracts import RecordSettings
from ..capture.names import station_component


class PreferencesError(ValueError):
    """Invalid or unreadable preferences cannot be overwritten implicitly."""


class ConflictError(PreferencesError):
    """The target changed, or another writer holds its lock."""


def default_preferences(root=None, mock=False):
    """Return fresh defaults; mock mode never consults production storage."""
    if root is None:
        root = (Path(tempfile.gettempdir()) / 'GNSS-GUI-MOCK' / uuid4().hex
                if mock else default_root())
    root = Path(root).resolve()
    return dict(schema_version=1, recording=dict(
        output=str(root/'internal/runs'), daily_root=None,
        station='MOCK' if mock else 'GNSS', baud=115200, expected_hz=1.0,
        seconds=60.0, retention_policy='keep_all', auto_configured=False,
        auto_enabled=False), history=dict(recording_roots=[str(root/'internal/runs')],
        conversion_roots=[str(root/'data/rinex')], stage1_roots=[]),
        interface=dict(polling_ms=250, geometry=None, page=0),
        conversion=dict(config_path=None))


def _path(value):
    if (not isinstance(value, str) or not value.strip() or '\x00' in value
            or not Path(value).is_absolute()):
        raise PreferencesError('Paths must be nonempty absolute strings')
    return value


def validate_preferences(values, defaults=None):
    """Merge partial fields with defaults and reject unknown or invalid fields."""
    # Defaults are input too: validate against the canonical schema rather than
    # letting supplied defaults redefine allowed keys, versions, or field types.
    base = default_preferences() if defaults is None else validate_preferences(defaults)
    if not isinstance(values, dict) or values.keys()-base.keys():
        raise PreferencesError('Unknown preferences fields or invalid object')
    if 'schema_version' in values and (type(values['schema_version']) is not int
                                      or values['schema_version'] != 1):
        raise PreferencesError('Unsupported preferences schema_version')
    for key, value in values.items():
        if key == 'schema_version':
            base[key] = value
        else:
            if not isinstance(value, dict) or value.keys()-base[key].keys():
                raise PreferencesError('Unknown or invalid preferences section: '+key)
            base[key].update(deepcopy(value))
    rec = base['recording']
    _path(rec['output'])
    if rec['daily_root'] is not None:
        _path(rec['daily_root'])
    # Reuse the recording contract with a transient validation-only binding.
    # This value is never returned, persisted, or used for receiver admission.
    from ..recording.contracts import DeviceBinding
    binding = DeviceBinding('ZED-F9P', 1, 1, usb_location='preferences-validation')
    try:
        RecordSettings(Path(rec['output']), 'preferences-validation', binding,
                       rec['seconds'], rec['baud'], rec['expected_hz'], rec['station'],
                       rec['retention_policy'],
                       Path(rec['daily_root']) if rec['daily_root'] is not None else None)
    except (TypeError, ValueError, OverflowError) as error:
        raise PreferencesError('Invalid recording preferences: '+str(error)) from error
    station_component(rec['station'])
    for key in ('auto_configured', 'auto_enabled'):
        if type(rec[key]) is not bool:
            raise PreferencesError(key+' must be boolean')
    if rec['auto_enabled'] and not rec['auto_configured']:
        raise PreferencesError('auto_enabled requires auto_configured')
    for paths in base['history'].values():
        if not isinstance(paths, list):
            raise PreferencesError('History roots must be lists')
        for value in paths:
            _path(value)
    interface = base['interface']
    if type(interface['polling_ms']) is not int or not 250 <= interface['polling_ms'] <= 2000:
        raise PreferencesError('polling_ms must be an integer from 250 to 2000')
    if type(interface['page']) is not int or not 0 <= interface['page'] <= 3:
        raise PreferencesError('page must be an integer from 0 to 3')
    geometry = interface['geometry']
    if geometry is not None:
        if not isinstance(geometry, dict) or set(geometry) != {'x','y','width','height','maximized'}:
            raise PreferencesError('Invalid geometry fields')
        if (any(type(geometry[k]) is not int for k in ('x','y','width','height'))
                or geometry['width'] <= 0 or geometry['height'] <= 0
                or type(geometry['maximized']) is not bool):
            raise PreferencesError('Invalid geometry values')
    if base['conversion']['config_path'] is not None:
        _path(base['conversion']['config_path'])
    return base


def fingerprint(path):
    """Content revision, including absence. Access errors propagate."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _locked(path):
    lock = path.with_name(path.name+'.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ConflictError('Another writer holds '+str(lock)) from error
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink()


def _atomic_write(path, payload, expected, check=None):
    """Caller holds lock. Validate the staged file before replacing the target."""
    if fingerprint(path) != expected:
        raise ConflictError('File changed; reload before saving')
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.',
                                         suffix='.tmp', delete=False) as handle:
            name = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if check is not None:
            check(name)
        if fingerprint(path) != expected:
            raise ConflictError('File changed while preparing save')
        os.replace(name, path)
    finally:
        if name is not None and name.exists():
            name.unlink()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreferencesError('Duplicate JSON key: '+key)
        result[key] = value
    return result


class PreferencesStore:
    def __init__(self, path, defaults=None):
        self.path = Path(path).resolve()
        self.defaults = validate_preferences(default_preferences() if defaults is None else defaults)
        self.warnings = []
        self.fingerprint = None
        self._loaded = False
        self._blocked = False
        self._last_valid = deepcopy(self.defaults)

    def load(self):
        self.warnings.clear()
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self.fingerprint = None
            self._loaded, self._blocked = True, False
            self._last_valid = deepcopy(self.defaults)
        except OSError as error:
            self._blocked = True
            self.warnings.append(str(error))
        else:
            self.fingerprint = hashlib.sha256(raw).hexdigest()
            self._loaded = True
            try:
                parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_object)
                if not isinstance(parsed, dict) or 'schema_version' not in parsed:
                    raise PreferencesError('Stored preferences require schema_version')
                self._last_valid = validate_preferences(parsed, self.defaults)
                self._blocked = False
            except (ValueError, TypeError, UnicodeError) as error:
                self._blocked = True
                self.warnings.append('Preferences preserved; use explicit recovery or a new target: '+str(error))
        return deepcopy(self._last_valid)

    def save(self, values, *, recover=False):
        validated = validate_preferences(values, self.defaults)
        if not self._loaded:
            self.load()
        if self._blocked and not recover:
            raise PreferencesError('Unreadable/invalid preferences preserved; use recover=True or a new target')
        payload = (json.dumps(validated, ensure_ascii=True, allow_nan=False, indent=2)+'\n').encode('utf-8')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path):
            if self._blocked:
                if fingerprint(self.path) != self.fingerprint or self.fingerprint is None:
                    raise ConflictError('Cannot recover an unreadable or changed file')
                backup = self.path.with_name(self.path.name+'.corrupt-'+uuid4().hex+'.bak')
                with backup.open('xb') as handle:
                    handle.write(self.path.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
                self.warnings.append('Original preferences preserved at '+str(backup))
            _atomic_write(self.path, payload, self.fingerprint,
                          lambda p: validate_preferences(json.loads(p.read_text('utf-8')), self.defaults))
            self.fingerprint = hashlib.sha256(payload).hexdigest()
            self._last_valid = deepcopy(validated)
            self._blocked = False
        return deepcopy(validated)
