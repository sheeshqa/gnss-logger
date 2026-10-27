"""Exercise the actual record CLI on paced SIMULATED F9P/X20P. Never opens COM."""
import argparse
import contextlib
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gnss_logger.cli import main
from gnss_logger.device.ubx import MON_VER_POLL
from gnss_logger.mock import MockReceiver,synthetic_epoch,synthetic_nav
from gnss_logger.recording.contracts import InputChunk
from gnss_logger.recording.controller import RecordController
from gnss_logger.recording.schema import validate_record


class SimulatedReader:
    simulated=True
    def __init__(self,connection_id,profile):
        self.connection_id,self.profile=connection_id,profile
        self.usb=dict(vid=0,pid=0,usb_serial='SIMULATED-'+profile,usb_location=None)
        self.mock=MockReceiver(profile)
        self.sequence=self.offset=self.epoch=0
        self.digest=hashlib.sha256()
        self.anchor=datetime.now(timezone.utc)
        self.next_epoch=time.monotonic()
        self.closed=False
    def start(self):self.mock.send(MON_VER_POLL)
    def read(self,remaining):
        if self.mock.responses:data=self.mock.responses.popleft()
        elif time.monotonic()>=self.next_epoch:
            data=synthetic_epoch(self.anchor+timedelta(seconds=self.epoch),self.mock.profile['signal_ids'])+synthetic_nav()
            self.epoch+=1;self.next_epoch+=1
        else:
            time.sleep(min(.02,remaining) if remaining is not None else .02)
            data=b''
        value=InputChunk(self.connection_id,self.sequence,self.offset,data,
                         datetime.now(timezone.utc).isoformat(),time.monotonic_ns())
        if data:self.sequence+=1;self.offset+=len(data);self.digest.update(data)
        return value
    def close(self):self.closed=True


def validate(output,seconds):
    output.mkdir(parents=True,exist_ok=False)
    summary=[]
    real_controller=RecordController
    for profile,model in (('f9p','ZED-F9P'),('x20p','ZED-X20P')):
        readers=[]
        def factory(settings,connection):
            reader=SimulatedReader(connection,profile);readers.append(reader);return reader
        def controller(**options):return real_controller(reader_factory=factory,**options)
        argv=['record','--output',str(output/profile),'--port','SIMULATED','--model',model,'--vid','0','--pid','0',
              '--usb-serial','SIMULATED-'+profile,'--seconds',str(seconds)]
        with patch('gnss_logger.recording.controller.RecordController',side_effect=controller),contextlib.redirect_stdout(io.StringIO()) as captured:
            code=main(argv)
        report=json.loads(captured.getvalue());validate_record(report)
        details=report['extensions']['gnss_logger.recording']
        manifest=json.loads(Path(details['paths']['manifest']).read_text(encoding='utf-8'));validate_record(manifest)
        binary=Path(manifest['artifacts'][0]['path'])
        digest=hashlib.sha256(binary.read_bytes()).hexdigest()
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            replay_code=main(['replay',str(binary),'--speed','0','--session-dir',str(output/'replay')])
        replay=json.loads(captured.getvalue())
        checks=dict(exit_ok=code==0 and replay_code==0,simulated=details['simulated'] is True,
            reader_closed=readers[0].closed,all_bytes=report['accounting']['received']==readers[0].offset==binary.stat().st_size,
            hashes=digest==readers[0].digest.hexdigest()==details['received_sha256']==replay['stream_sha256'],
            no_loss=report['accounting']['pending']==report['accounting']['unarchived']==0,
            synced=report['accounting']['journal_synced']==binary.stat().st_size,
            messages=replay['messages']==report['extensions']['gnss_logger.diagnostics']['messages'],
            stop=manifest['stop_reason']=='duration',single_primary=len(manifest['artifacts'])==1)
        if not all(checks.values()):raise RuntimeError(str(checks))
        summary.append(dict(profile=profile,checks=checks,bytes=binary.stat().st_size,sha256=digest,
            rawx=replay['RAWX']['epochs'],sfrbx=replay['SFRBX']['messages'],manifest=details['paths']['manifest'],report=details['paths']['diagnostic']))
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True,help='New SIMULATED output directory outside source data')
    p.add_argument('--seconds',type=float,default=2.2)
    args=p.parse_args()
    import math
    if not math.isfinite(args.seconds) or args.seconds<1.1:p.error('Use at least 1.1 seconds')
    validate(args.output,args.seconds)
