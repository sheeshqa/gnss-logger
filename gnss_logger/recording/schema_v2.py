"""Additive daily-projection contract. Version 1 remains byte-for-byte unchanged."""
from .schema import obj, nullable, TEXT, UINT, SHA, UUID, UTC

CONTRACT_V2 = 'stage2.3/v2'
INDEX = obj(dict(path=TEXT, bytes=UINT, sha256=SHA))
PROJECTION = obj(dict(
    root=TEXT, cursor=UINT, eligible_bytes=UINT, projected_bytes=UINT,
    opaque_bytes=UINT, tail_bytes=UINT, pending_bytes=UINT, backlog_bytes=UINT,
    receipt_frontier=UINT, receipt_index=nullable(INDEX), range_index=nullable(INDEX),
    artifact_directory=TEXT, closed_artifacts=UINT, verified_artifacts=UINT,
    verification_pending=UINT, failed=dict(type='boolean'), finished=dict(type='boolean'),
    time_state=dict(enum=['UNDATED', 'CANDIDATE', 'VALID', 'DISCONTINUITY']), generation=UINT,
    time_policy=obj(dict(period_s=dict(type='number', exclusiveMinimum=0),
                         ttl_s=dict(type='number', exclusiveMinimum=0),
                         tolerance_s=dict(type='number', exclusiveMinimum=0), anchors=dict(const=2))),
    time_checkpoint=obj({}, required=[], extra=True),
    active=nullable(obj({}, required=[], extra=True))))
EVENT_DETAILS_V2 = {name: obj(dict(artifact_id=nullable(UUID), source_offset=UINT,
    day=nullable(TEXT), previous_day=nullable(TEXT), generation=UINT,
    message=dict(type='string'))) for name in (
        'SEGMENT_OPENED', 'ROTATION', 'TIME_BINDING', 'SEGMENT_CLOSED', 'PROJECTION_ERROR')}

OWNERS = dict(run_id=UUID,attempt_id=UUID,connection_id=UUID,journal_id=UUID)
ROW_SPECS = {
    'artifact': dict(**OWNERS,artifact_id=UUID,path=TEXT,final_path=TEXT,
        kind=dict(enum=['DAILY','UNDATED']),utc_day=nullable(dict(type='string',format='date')),
        generation=UINT,state=dict(enum=['OPEN','CLOSED_COMPLETE','CLOSED_PARTIAL','UNVERIFIED']),
        bytes=UINT,synced_bytes=UINT,sha256=nullable(SHA),synced_sha256=SHA,error=nullable(TEXT),
        first_rawx_utc=nullable(UTC),last_rawx_utc=nullable(UTC),source_start=UINT,source_end=UINT,
        partial_day=dict(const=True),close_reason=nullable(TEXT)),
    'receipt': dict(**OWNERS,sequence=UINT,start=UINT,end=UINT,received_utc=UTC,monotonic_ns=UINT),
    'range': dict(**OWNERS,start=UINT,end=UINT,kind=dict(enum=['FRAME','OPAQUE','TAIL']),
        artifact_id=nullable(UUID),destination_offset=nullable(UINT)),
}


def get_schema_v2(kind):
    from .schema import get_schema
    if kind in ROW_SPECS:
        return dict({'$schema':'https://json-schema.org/draft/2020-12/schema',
                     '$id':'urn:gnss-logger:stage2:'+kind+':2'},
                    **obj(dict(schema_id=dict(const='gnss_logger.stage2.'+kind),
                        schema_version=dict(type='integer',const=2),
                        contract_version=dict(const=CONTRACT_V2),**ROW_SPECS[kind])))
    schema = get_schema(kind, 1)
    schema['$id'] = 'urn:gnss-logger:stage2:' + kind + ':2'
    properties = schema['properties']
    properties['schema_version']['const'] = 2
    properties['contract_version']['const'] = CONTRACT_V2
    if kind == 'event':
        properties['kind']['enum'] += list(EVENT_DETAILS_V2)
        schema['allOf'] += [{'if': {'properties': {'kind': {'const': name}}},
                            'then': {'properties': {'details': spec}}}
                           for name, spec in EVENT_DETAILS_V2.items()]
    else:
        properties['projection'] = PROJECTION
        schema['required'].append('projection')
    return schema


def invariants(record):
    kind=record['schema_id'].split('.')[-1]
    if kind in ('range','receipt'):
        if record['end'] <= record['start']:
            raise ValueError('Index ranges must be nonempty')
    if kind == 'range':
        frame=record['kind']=='FRAME'
        if frame != (record['artifact_id'] is not None and record['destination_offset'] is not None):
            raise ValueError('Only FRAME ranges have destinations')
    if kind == 'artifact':
        if ((record['kind']=='DAILY') != (record['utc_day'] is not None)
                or record['synced_bytes'] > record['bytes'] or record['source_end'] < record['source_start']):
            raise ValueError('Invalid derived artifact coverage')
        if record['state'] in ('CLOSED_COMPLETE','CLOSED_PARTIAL') and (record['sha256'] is None
                or record['synced_bytes'] != record['bytes']):
            raise ValueError('Closed derived files require synced bytes/hash')
    p = record.get('projection')
    if p is None:
        return
    if p['projected_bytes'] + p['pending_bytes'] != p['eligible_bytes']:
        raise ValueError('Every eligible byte needs a projection or a pending range')
    if p['cursor'] != p['eligible_bytes'] + p['opaque_bytes'] + p['tail_bytes']:
        raise ValueError('Projection classification must cover the cursor')
    if p['cursor'] > p['receipt_frontier']:
        raise ValueError('Projection cannot pass receipt frontier')
    if p['verified_artifacts'] > p['closed_artifacts']:
        raise ValueError('Verified artifacts exceed closed artifacts')
    frontier = record.get('synced_offset', record.get('accounting', {}).get('journal_synced'))
    if frontier is not None and p['receipt_frontier'] > frontier:
        raise ValueError('Receipt frontier cannot exceed synced primary')
    ledger=record.get('accounting')
    if ledger and (p['cursor']+p['backlog_bytes'] != ledger['journal_written']
                   or p['eligible_bytes'] > ledger['framed']):
        raise ValueError('Projection must agree with primary accounting')
    if record.get('exit_code') == 0 and (p['failed'] or not p['finished'] or p['pending_bytes']
            or p['backlog_bytes'] or p['verification_pending']
            or p['verified_artifacts'] != p['closed_artifacts']):
        raise ValueError('Successful run requires committed, verified projection')
