"""Resolve completed segments from Stage 2 manifests or explicit paths."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from ..recording.schema import validate_record
from ..recording.contracts import utc


@dataclass(frozen=True)
class Source:
    path: Path
    run_id: str = None
    attempt_id: str = None
    artifact_id: str = None
    sha256: str = None
    size: int = None
    utc_day: str = None
    receiver_model: str = None


def file_source(path, **metadata):
    if path.suffix.lower()!='.ubx' or path.is_symlink():
        raise ValueError('Expected a completed regular .ubx file: '+str(path))
    path = path.resolve(strict=True)
    if path.suffix.lower()!='.ubx' or not path.is_file() or path.stat().st_size==0:
        raise ValueError('Missing or empty completed UBX: '+str(path))
    return Source(path,**metadata)


def checked_path(raw, root):
    path = Path(raw)
    if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Manifest path lies outside configured root: '+str(path))
    return path


def read(path):
    return validate_record(json.loads(path.read_text(encoding='utf-8-sig')))


def select(settings, input_path=None, run_id=None):
    if input_path is not None:
        path = Path(input_path)
        if not path.is_dir():
            return [file_source(path)]
        files = sorted((p for p in path.iterdir() if p.suffix.lower()=='.ubx'),
                       key=lambda p:p.name.casefold())
        if not files:
            raise ValueError('Directory contains no completed .ubx files')
        return [file_source(p) for p in files]
    if run_id and not re.fullmatch(r'[a-f0-9]{32}',run_id):
        raise ValueError('run_id must be a lowercase UUID hex string')
    pattern = (run_id or '*')+'/attempts/*/manifest.json'
    candidates = []
    for path in settings.runs_root.glob(pattern):
        if not path.resolve().is_relative_to(settings.runs_root.resolve()):
            raise ValueError('Manifest escapes runs_root')
        manifest = read(path)
        if manifest['run_id']!=path.parents[2].name or manifest['attempt_id']!=path.parent.name:
            raise ValueError('Manifest identifiers disagree with its directory')
        if (manifest['state']=='STOPPED' and manifest['exit_code']==0
                and manifest['integrity']=='VERIFIED'):
            candidates.append((utc(manifest['ended_utc']),str(path),manifest,path.parent))
    if not candidates:
        raise ValueError('No successful terminal manifest. Use --input for archived UBX')
    _, _, manifest, directory = max(candidates,key=lambda item:item[:2])
    owners = dict(run_id=manifest['run_id'],attempt_id=manifest['attempt_id'])
    if manifest['schema_version']==2:
        projection = manifest['projection']
        artifacts = checked_path(projection['artifact_directory'],directory)
        rows = []
        for path in artifacts.glob('*.json'):
            if path.name.endswith(('.open.json','.failed.json')):
                continue
            row = read(checked_path(str(path),artifacts))
            if (row['schema_id']!='gnss_logger.stage2.artifact' or
                row['state']!='CLOSED_COMPLETE' or path.stem!=row['artifact_id'] or
                any(row[k]!=v for k,v in owners.items())):
                raise ValueError('Unexpected artifact metadata: '+str(path))
            if not any(a['kind']=='PRIMARY' and a['artifact_id']==row['journal_id']
                       and a['connection_id']==row['connection_id'] for a in manifest['artifacts']):
                raise ValueError('Artifact is not owned by the selected primary journal')
            rows.append(row)
        if len(rows)!=projection['verified_artifacts'] or not rows:
            raise ValueError('Completed projection artifact count mismatch')
        rows.sort(key=lambda row:row['source_start'])
        root = settings.daily_root
    else:
        rows = [a for a in manifest['artifacts'] if a['kind']=='PRIMARY'
                and a['state']=='CLOSED_COMPLETE']
        root = directory
    if not rows:
        raise ValueError('No completed UBX artifacts in selected manifest')
    models = {c['connection_id']:c['observed_model'] for c in manifest['connections']}
    return [file_source(checked_path(row['path'],root),**owners,artifact_id=row['artifact_id'],
                        sha256=row['sha256'],size=row['bytes'],utc_day=row['utc_day'],
                        receiver_model=models.get(row['connection_id'])) for row in rows]
