"""Commit and verify one conversion result at its final destination."""
import json
import os
from pathlib import Path

from ..recording.storage import publish_exclusive
from ..session import file_fingerprint, write_new_json

SUCCESS = {'SUCCESS', 'SUCCESS_WITH_WARNINGS', 'ALREADY_CONVERTED'}
OBS_ONLY = 'OBS_ONLY_INCOMPLETE_NAVIGATION'


def identity(settings, segment, engine):
    key = dict(source_sha256=segment['source']['sha256'], source_bytes=segment['source']['bytes'],
                executable_sha256=engine['sha256'], arguments=segment['args'][1:],
                interval_s=settings.interval_s)
    if settings.allow_obs_without_nav:
        key['allow_obs_without_nav']=True
    return key


def existing(receipt, key, outputs):
    if receipt.is_symlink():
        return None
    try:
        value = json.loads(receipt.read_text(encoding='utf-8'))
        if not isinstance(value,dict):
            return None
        expected_outputs=outputs
        if value.get('status')==OBS_ONLY:
            if not key.get('allow_obs_without_nav') or 'obs' not in outputs:
                return None
            if 'nav' in outputs and os.path.lexists(outputs['nav']):
                return None
            expected_outputs={'obs':outputs['obs']}
        if (value['schema_id']!='gnss_logger.conversion.result' or value['schema_version']!=1
                or value['identity']!=key or value['published_outputs']!=expected_outputs
                or value['status'] not in {'SUCCESS','SUCCESS_WITH_WARNINGS',OBS_ONLY}
                or (value['status']==OBS_ONLY and value.get('missing_outputs')!=['nav'])):
            return None
        if not all(not Path(path).is_symlink() and file_fingerprint(Path(path))==value['output_fingerprints'][kind]
                   for kind,path in expected_outputs.items()):
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def legacy(root, key, outputs):
    """Stage 3.1 reports can prove an older pair using the same provenance."""
    for path in root.glob('conversion-*.json'):
        if path.is_symlink():
            continue
        try:
            report=json.loads(path.read_text(encoding='utf-8'))
            if (report['schema_id']!='gnss_logger.conversion' or report['schema_version']!=1
                    or report['dry_run']):
                continue
            for segment in report['segments']:
                if segment['status'] not in {'SUCCESS','SUCCESS_WITH_WARNINGS'} or segment['published_outputs']!=outputs:
                    continue
                old_key=dict(source_sha256=segment['source']['sha256'],source_bytes=segment['source']['bytes'],
                             executable_sha256=report['converter']['sha256'],arguments=segment['args'][1:],
                             interval_s=report['settings']['interval_s'])
                if old_key==key and all(not Path(target).is_symlink() and
                        file_fingerprint(Path(target))==segment['output_fingerprints'][kind]
                        for kind,target in outputs.items()):
                    return segment, str(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, None


def commit(work, receipt, key, segment):
    # The receipt is the commit marker. Prepare it before exposing any output.
    staged = work/'result.json'
    status = OBS_ONLY if segment.get('status')==OBS_ONLY else (
        'SUCCESS_WITH_WARNINGS' if segment['warnings'] else 'SUCCESS')
    value = {**segment, 'schema_id':'gnss_logger.conversion.result','schema_version':1,
             'identity':key,'published_outputs':segment['outputs'], 'status':status}
    for kind in segment['outputs']:
        with (work/('output.'+kind)).open('rb+') as output:
            os.fsync(output.fileno())
    write_new_json(staged,value)
    published = []
    try:
        for kind, name in segment['outputs'].items():
            target = Path(name)
            publish_exclusive(work/('output.'+kind), target)
            published.append(target)
        publish_exclusive(staged, receipt)
    except BaseException:
        for path in published:
            path.unlink()
        raise
    segment.update(status=status, published_outputs=segment['outputs'].copy(), result_path=str(receipt))
