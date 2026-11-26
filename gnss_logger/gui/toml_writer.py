"""Explicit serializer for the existing conversion schema; no TOML dependencies.

Absolute paths preserve loaded Settings semantics when saving under a new name.
Formatting/comments are lost; the UI must confirm that warning before overwrite.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from ..conversion.config import Settings, load
from .preferences import ConflictError, _atomic_write, _locked, fingerprint

_UNSET = object()
COMMENTS_WARNING = 'Saving rewrites TOML formatting and removes comments; confirm before overwrite.'


def _value(value):
    if isinstance(value, str):
        # Quote each scalar separately so literal backslash-u text is preserved.
        # JSON surrogate pairs are not legal TOML Unicode scalar escapes.
        parts = []
        for char in value:
            code = ord(char)
            if 0xD800 <= code <= 0xDFFF:
                raise ValueError('Unpaired Unicode surrogate in TOML string')
            parts.append('\\U%08x' % code if code > 0xFFFF else
                         json.dumps(char, ensure_ascii=True)[1:-1])
        return '"'+''.join(parts)+'"'
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) in (int, float):
        return repr(value)
    if isinstance(value, tuple):
        return '['+', '.join(_value(item) for item in value)+']'
    raise ValueError('Unsupported TOML value type')


def serialize(settings):
    """Validate Settings and emit every supported conversion key."""
    if not isinstance(settings, Settings):
        raise ValueError('Expected conversion Settings')
    settings = replace(settings)  # Run the current schema validation again.
    paths = {}
    for key in ('executable', 'runs_root', 'daily_root', 'output_root'):
        path = getattr(settings, key)
        if not path.is_absolute() or '\x00' in str(path):
            raise ValueError(key+' must be an absolute path')
        paths[key] = str(path)
    sections = dict(
        engine=dict(executable=paths['executable'], input_format='ubx',
                    receiver_model=settings.receiver_model, timeout_s=settings.timeout_s),
        input=dict(runs_root=paths['runs_root'], daily_root=paths['daily_root'],
                   interval_s=settings.interval_s),
        output=dict(root=paths['output_root'], obs=settings.obs, nav=settings.nav,
                    allow_obs_without_nav=settings.allow_obs_without_nav),
        rinex=dict(version=settings.version, systems=settings.systems,
                   observations=settings.observations, signal_groups=settings.groups,
                   signal_masks=settings.masks))
    return '\n\n'.join('['+section+']\n'+'\n'.join(
        key+' = '+_value(value) for key, value in values.items())
        for section, values in sections.items())+'\n'


serialize_settings = serialize


class TomlStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.fingerprint = None
        self.warnings = [COMMENTS_WARNING]
        self._loaded = False

    def load(self):
        before = fingerprint(self.path)
        settings = load(self.path)
        if fingerprint(self.path) != before:
            raise ConflictError('TOML changed during load')
        self.fingerprint = before
        self._loaded = True
        return settings

    def save(self, settings, overwrite=False, expected_fingerprint=_UNSET):
        """Overwrite is caller confirmation of comment loss; no retries.

        Omitted revision uses this instance's last load/save. Explicit None
        requires an absent target. Existing targets require a known revision.
        """
        payload = serialize(settings).encode('utf-8')
        expected = (self.fingerprint if self._loaded else None) if expected_fingerprint is _UNSET else expected_fingerprint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path):
            current = fingerprint(self.path)
            if current is not None:
                if not overwrite:
                    raise FileExistsError(self.path)
                # Never discard unsupported sections, even with confirmation.
                load(self.path)
            if current != expected:
                raise ConflictError('TOML changed or has not been loaded; reload before saving')
            def roundtrip(path):
                if load(path) != settings:
                    raise ValueError('Serialized TOML did not round-trip to the supplied Settings')
            _atomic_write(self.path, payload, expected, roundtrip)
            self.fingerprint = hashlib.sha256(payload).hexdigest()
            self._loaded = True
        return settings


def save_settings(path, settings, overwrite=False, expected_fingerprint=_UNSET):
    """Functional equivalent; existing targets require an explicit fingerprint."""
    return TomlStore(path).save(settings, overwrite, expected_fingerprint)
