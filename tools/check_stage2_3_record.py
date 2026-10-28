"""Offline verification and explicitly simulated daily Replay. Never opens COM."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gnss_logger.capture.framing import Framer
from gnss_logger.device.ubx import rawx
from gnss_logger.recording.contracts import InputChunk
from gnss_logger.recording.projection import Projection
from gnss_logger.recording.schema import validate_record
from gnss_logger.recording.storage import PrimaryJournal
from gnss_logger.session import file_fingerprint, write_new_json


def load(path):
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    validate_record(value)
    return value


def check_projection(primary, projection, owners):
    """Stream range coverage and original-byte comparison, including opaque/tail."""
    if projection['failed'] or not projection['finished'] or projection['pending_bytes'] or projection['backlog_bytes']:
        raise ValueError('Projection is incomplete/failed')
    artifact_dir=Path(projection['artifact_directory'])
    artifacts=0
    for path in artifact_dir.glob('*.json'):
        if '.open.' in path.name or '.failed.' in path.name:
            continue
        artifact=load(path)
        if artifact['state'] not in ('CLOSED_COMPLETE','CLOSED_PARTIAL'):
            raise ValueError('Artifact was not closed and verified')
        if any(artifact[k] != v for k,v in owners.items()):
            raise ValueError('Artifact owner mismatch')
        if file_fingerprint(Path(artifact['path'])) != dict(bytes=artifact['bytes'],sha256=artifact['sha256']):
            raise ValueError('Artifact SHA/size mismatch')
        artifacts+=1
    if artifacts != projection['verified_artifacts'] or artifacts != projection['closed_artifacts']:
        raise ValueError('Closed artifact count mismatch')
    for name in ('receipt_index','range_index'):
        reference=projection[name]
        if file_fingerprint(Path(reference['path'])) != dict(bytes=reference['bytes'],sha256=reference['sha256']):
            raise ValueError(name+' SHA/size mismatch')
    receipt_end=sequence=0
    with Path(projection['receipt_index']['path']).open(encoding='utf-8') as handle:
        for line in handle:
            receipt=json.loads(line);validate_record(receipt)
            if (any(receipt[k]!=v for k,v in owners.items()) or receipt['start']!=receipt_end
                    or receipt['sequence']!=sequence):
                raise ValueError('Receipt owner/sequence/coverage mismatch')
            receipt_end=receipt['end'];sequence+=1
    if receipt_end!=primary.stat().st_size or projection['receipt_frontier']!=receipt_end:
        raise ValueError('Receipt frontier does not cover primary')
    expected=framed=opaque=tail=0
    active_id=active_record=None
    destination=0
    with primary.open('rb') as original, Path(projection['range_index']['path']).open(encoding='utf-8') as index:
        for line in index:
            span=json.loads(line);validate_record(span)
            if any(span[k] != v for k,v in owners.items()) or span['start'] != expected:
                raise ValueError('Range owner/coverage/order mismatch')
            length=span['end']-span['start']
            if span['kind']=='FRAME':
                if active_id != span['artifact_id']:
                    if active_record and destination != active_record['bytes']:
                        raise ValueError('Previous artifact not fully covered')
                    active_id=span['artifact_id'];destination=0
                    active_record=load(artifact_dir/(active_id+'.json'))
                    if active_record['source_start'] != span['start']:
                        raise ValueError('Artifact source start mismatch')
                if destination != span['destination_offset']:
                    raise ValueError('Destination offset mismatch')
                with Path(active_record['path']).open('rb') as derived:
                    original.seek(span['start']);derived.seek(destination)
                    left=length
                    while left:
                        count=min(65536,left)
                        source=original.read(count);target=derived.read(count)
                        if len(source)!=count or source!=target:
                            raise ValueError('Derived bytes differ from primary slice')
                        left-=count
                destination+=length;framed+=length
            elif span['kind']=='OPAQUE':opaque+=length
            else:tail+=length
            expected=span['end']
    if active_record and destination != active_record['bytes']:
        raise ValueError('Last artifact not fully covered')
    if expected != primary.stat().st_size or (framed,opaque,tail) != (
            projection['projected_bytes'],projection['opaque_bytes'],projection['tail_bytes']):
        raise ValueError('Primary classification/size mismatch')
    # Independent full-packet validation of every derived file, bounded buffers.
    frames=rawx_count=sfrbx_count=0
    for path in artifact_dir.glob('*.json'):
        if '.open.' in path.name or '.failed.' in path.name:continue
        artifact=load(path);framer=Framer()
        with Path(artifact['path']).open('rb') as handle:
            while data:=handle.read(4096):
                for _,_,frame in framer.feed(data):
                    if frame is None:raise ValueError('Non-frame bytes in derived UBX')
                    frames+=1
                    if frame[:2]==(2,0x15):
                        rawx_count+=1
                        if artifact['utc_day']:
                            from gnss_logger.timebase import rawx_utc
                            epoch=rawx(frame[2]);stamp=rawx_utc(epoch)
                            if stamp is None or stamp.date().isoformat()!=artifact['utc_day']:
                                raise ValueError('RAWX observation day differs from artifact day')
                    if frame[:2]==(2,0x13):sfrbx_count+=1
        if framer.tail()[1]:raise ValueError('Partial UBX in derived file')
    return dict(status='PASS',verified_artifacts=artifacts,primary_bytes=expected,
                projected_bytes=framed,opaque_bytes=opaque,tail_bytes=tail,frames=frames,
                rawx=rawx_count,sfrbx=sfrbx_count)


def verify(attempt):
    manifest=load(attempt/'manifest.json')
    if manifest['schema_version']!=2 or manifest['state']!='STOPPED':
        raise ValueError('A terminal Stage 2.3 manifest is required')
    primary=next(a for a in manifest['artifacts'] if a['kind']=='PRIMARY')
    if file_fingerprint(Path(primary['path'])) != dict(bytes=primary['bytes'],sha256=primary['sha256']):
        raise ValueError('Primary SHA/size mismatch')
    owners=dict(run_id=manifest['run_id'],attempt_id=manifest['attempt_id'],
                connection_id=primary['connection_id'],journal_id=primary['artifact_id'])
    result=check_projection(Path(primary['path']),manifest['projection'],owners)
    result.update(verification_scope='file_integrity; not hardware/scientific acceptance',
                  registration_exit_code=manifest['exit_code'],stop_reason=manifest['stop_reason'])
    return result


def replay(source, output):
    """Logical arrival time from RAWX is SIMULATED: historic USB receipt times unknown."""
    before=file_fingerprint(source)
    output.mkdir(parents=True,exist_ok=False)
    owners=dict(zip(('run_id','attempt_id','connection_id','journal_id'),(uuid.uuid4().hex for _ in range(4))))
    journal=PrimaryJournal(output/'replay-primary.ubx.part')
    p=None
    try:
        p=Projection(output,output/'data','UBX1',owners,journal.path,1,lambda *args:None)
        sequence=offset=virtual_ns=0;last_gps=None
        def append(data):
            nonlocal sequence,offset
            for pos in range(0,len(data),4096):
                chunk=InputChunk(owners['connection_id'],sequence,offset,data[pos:pos+4096],
                    datetime.now(timezone.utc).isoformat(),virtual_ns)
                journal.append(chunk);p.primary_end=offset+len(chunk.data);p.receipt(chunk,len(chunk.data))
                sequence+=1;offset+=len(chunk.data)
        framer=Framer()
        with source.open('rb') as handle:
            while data:=handle.read(4096):
                for _,original,frame in framer.feed(data):
                    if frame and frame[:2]==(2,0x15):
                        try:
                            epoch=rawx(frame[2]);gps=epoch['week']*604800+epoch['tow_s']
                            if last_gps is not None:virtual_ns+=max(0,int((gps-last_gps)*1e9))
                            last_gps=gps
                        except ValueError:pass
                    append(original)
                if journal.written-journal.synced>=65536:
                    journal.sync();p.sync_receipts(journal.synced);p.advance();p.checkpoint()
            append(framer.tail()[1])
        journal.sync();p.sync_receipts(journal.synced);p.finish(True,'offline_replay')
        closed=journal.close('source_exhausted')
        if closed['failures']:raise OSError(str(closed['failures']))
        if closed['fingerprint']!=before or file_fingerprint(source)!=before:
            raise ValueError('Replay primary/source hash mismatch')
        result=check_projection(Path(closed['artifact']['path']),p.snapshot(),owners)
        result.update(simulated=True,hardware_validation='NOT TESTED',
            historical_arrival_unknown=True,source=str(source.resolve()),source_fingerprint=before,
            time_basis='SIMULATED monotonic intervals from RAWX; never historic USB timings')
        write_new_json(output/'replay-check.json',result)
        return result
    finally:
        if p and p.verifier.thread.is_alive():
            p.failed=True
            try:p.finish(False,'offline_replay_error')
            except Exception:pass
        if not journal.handle.closed:journal.handle.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('verify').add_argument('attempt',type=Path)
    replay_parser=sub.add_parser('replay');replay_parser.add_argument('source',type=Path)
    replay_parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=verify(args.attempt) if args.command=='verify' else replay(args.source,args.output)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as error:
        print(json.dumps(dict(status='FAIL',error_type=type(error).__name__,error=str(error)),ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
