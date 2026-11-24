"""TOML configuration and the supported UBX signal profile."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil

# System, RTKLIB observation code, frequency slot (one-based).
SIGNALS = {
    (0, 0): ('G', '1C', 1), (0, 3): ('G', '2L', 2), (0, 4): ('G', '2S', 2),
    (0, 6): ('G', '5I', 3), (0, 7): ('G', '5Q', 3),
    (6, 0): ('R', '1C', 1), (6, 2): ('R', '2C', 2),
    (2, 0): ('E', '1C', 1), (2, 1): ('E', '1B', 1),
    (2, 3): ('E', '5I', 3), (2, 4): ('E', '5Q', 3),
    (2, 5): ('E', '7I', 2), (2, 6): ('E', '7Q', 2),
    (2, 8): ('E', '6B', 4), (2, 9): ('E', '6C', 4), (2, 10): ('E', '6A', 4),
    (3, 0): ('C', '2I', 1), (3, 1): ('C', '2I', 1),
    (3, 2): ('C', '7I', 2), (3, 3): ('C', '7I', 2),
    (3, 4): ('C', '6I', 4), (3, 10): ('C', '6I', 4),
}
SYSTEMS = 'GREC'
GNSS = {0: 'G', 2: 'E', 3: 'C', 6: 'R'}
GROUPS = ('L1', 'L2/E5b', 'L5/E5a', 'L6')
MASKS = {system+'L'+code: slot for system, code, slot in SIGNALS.values()}
COMBINED = {
    ('G','2L'):'2X', ('G','2S'):'2X', ('G','5I'):'5X', ('G','5Q'):'5X',
    ('E','1B'):'1X', ('E','1C'):'1X', ('E','5I'):'5X', ('E','5Q'):'5X',
    ('E','7I'):'7X', ('E','7Q'):'7X', ('E','6A'):'6X', ('E','6B'):'6X', ('E','6C'):'6X',
}
MASKS.update({system+'L'+combined: MASKS[system+'L'+code]
              for (system,code),combined in COMBINED.items()})


def default_root():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local/share')/'GNSS'


@dataclass(frozen=True)
class Settings:
    executable: Path
    runs_root: Path
    daily_root: Path
    output_root: Path
    version: str
    systems: tuple
    observations: tuple
    groups: tuple
    masks: tuple
    obs: bool
    nav: bool
    receiver_model: str
    timeout_s: float
    interval_s: float
    allow_obs_without_nav: bool = False

    def __post_init__(self):
        for name in ('executable', 'runs_root', 'daily_root', 'output_root'):
            if not isinstance(getattr(self, name), Path):
                raise ValueError(name+' must be a Path')
        if self.version != '3.02':
            raise ValueError('Stage 3.1 supports exactly RINEX 3.02')
        for name, allowed in (('systems', SYSTEMS), ('observations', 'CLDS'), ('groups', GROUPS)):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not value or any(not isinstance(x, str) or x not in allowed for x in value) or len(set(value)) != len(value):
                raise ValueError('Invalid '+name)
        if not {'C', 'L'} <= set(self.observations):
            raise ValueError('convbin always enables code and phase. Include C and L')
        allowed = {mask for mask, slot in MASKS.items()
                   if mask[0] in self.systems and GROUPS[slot-1] in self.groups}
        if (not isinstance(self.masks, tuple) or not self.masks or
                any(not isinstance(mask, str) or mask not in allowed for mask in self.masks) or
                len(set(self.masks)) != len(self.masks) or
                set(self.systems)-{mask[0] for mask in self.masks}):
            raise ValueError('Invalid signal masks for selected systems/groups')
        if type(self.obs) is not bool or type(self.nav) is not bool or not (self.obs or self.nav):
            raise ValueError('Enable OBS, NAV, or both')
        if type(self.allow_obs_without_nav) is not bool or (self.allow_obs_without_nav and not (self.obs and self.nav)):
            raise ValueError('allow_obs_without_nav requires OBS and NAV enabled and a boolean value')
        if not isinstance(self.receiver_model, str) or len(self.receiver_model)>20 or any(
                ord(c)<32 or ord(c)>126 or c=='/' for c in self.receiver_model):
            raise ValueError('Invalid receiver_model')
        for name in ('timeout_s', 'interval_s'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value<=0:
                raise ValueError(name+' must be finite and positive')


def load(path=None):
    base = default_root()
    config = Path(path) if path else base/'conversion.local.toml'
    data = {}
    if path or config.exists():
        try:
            import tomllib as toml
        except ImportError:
            import tomli as toml
        with config.open('rb') as handle:
            data = toml.load(handle)
    keys = dict(engine={'executable','input_format','receiver_model','timeout_s'},
                input={'runs_root','daily_root','interval_s'},
                output={'root','obs','nav','allow_obs_without_nav'},
                rinex={'version','systems','observations','signal_groups','signal_masks'})
    for key, section in data.items():
        if key not in keys or not isinstance(section, dict) or section.keys()-keys[key]:
            raise ValueError('Unknown or invalid configuration section: '+key)
    engine, source, output, rinex = (data.get(k, {}) for k in keys)

    def location(value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError('Configuration path must be nonempty')
        expanded = Path(os.path.expandvars(str(value))).expanduser()
        return (config.resolve().parent/expanded).resolve()

    def choices(value, allowed, name):
        if not isinstance(value, (list, tuple)) or not value or any(
                not isinstance(x,str) or x not in allowed for x in value) or len(set(value))!=len(value):
            raise ValueError('Invalid '+name)
        return tuple(value)

    def positive(value, name):
        if type(value) not in (int, float) or not math.isfinite(value) or value<=0:
            raise ValueError(name+' must be finite and positive')
        return value

    if engine.get('input_format','ubx') != 'ubx':
        raise ValueError('Only UBX input is supported')
    version = rinex.get('version','3.02')
    if version != '3.02':
        raise ValueError('Stage 3.1 supports exactly RINEX 3.02')
    systems = choices(rinex.get('systems',tuple(SYSTEMS)), SYSTEMS, 'systems')
    observations = choices(rinex.get('observations',tuple('CLDS')), 'CLDS', 'observations')
    if not {'C','L'} <= set(observations):
        raise ValueError('convbin always enables code and phase. Include C and L')
    groups = choices(rinex.get('signal_groups',GROUPS),GROUPS,'signal_groups')
    masks = tuple(mask for mask, slot in MASKS.items()
                  if mask[0] in systems and GROUPS[slot-1] in groups)
    if 'signal_masks' in rinex:
        masks = choices(rinex['signal_masks'],masks,'signal_masks for selected systems/groups')
    if not masks or set(systems)-{mask[0] for mask in masks}:
        raise ValueError('Each selected system needs at least one supported signal mask')
    obs, nav = output.get('obs',True), output.get('nav',True)
    if type(obs) is not bool or type(nav) is not bool or not (obs or nav):
        raise ValueError('Enable OBS, NAV, or both')
    allow_obs_without_nav=output.get('allow_obs_without_nav',False)
    if type(allow_obs_without_nav) is not bool or (allow_obs_without_nav and not (obs and nav)):
        raise ValueError('allow_obs_without_nav requires OBS and NAV enabled and a boolean value')
    model = engine.get('receiver_model','')
    if not isinstance(model,str) or len(model)>20 or any(ord(c)<32 or ord(c)>126 or c=='/' for c in model):
        raise ValueError('receiver_model must be at most 20 printable ASCII characters without slash')
    executable = engine.get('executable',shutil.which('convbin') or str(base/'tools/convbin.exe'))
    return Settings(location(executable),location(source.get('runs_root',base/'internal/runs')),
        location(source.get('daily_root',base/'data')),location(output.get('root',base/'data/rinex')),
        version,systems,observations,groups,masks,obs,nav,model,
        positive(engine.get('timeout_s',1800),'timeout_s'),positive(source.get('interval_s',1),'interval_s'),
        allow_obs_without_nav)
