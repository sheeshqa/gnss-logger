"""Independent conversion entry point and per-operation JSON reports."""
from dataclasses import asdict
import json
from collections import Counter
from pathlib import Path
import subprocess
import uuid

from .. import __version__
from ..session import utc_now, write_new_json
from ..recording.storage import publish_exclusive
from .config import default_root, load
from .engine import boundaries, convert, probe, stage_report
from .selection import select
from .results import SUCCESS

HANDLED = (OSError, ValueError, subprocess.SubprocessError, KeyError, ImportError)


def add_command(sub):
    command=sub.add_parser('convert',help='Convert completed UBX segments to RINEX 3.02')
    source=command.add_mutually_exclusive_group()
    source.add_argument('--input',type=Path,help='Completed UBX file or directory of UBX files')
    source.add_argument('--run-id',help='Completed recording run UUID')
    command.add_argument('--config',type=Path,help='TOML settings')
    command.add_argument('--output',type=Path,help='Root for unique per-operation OBS/NAV and report directories')
    command.add_argument('--dry-run',action='store_true',help='Show arguments and destinations without publishing files')


def execute(args, sources=None):
    operation=uuid.uuid4().hex
    root=(args.output or default_root()/'data/rinex').resolve()/('conversion-'+operation)
    report=dict(schema_version=2,schema_id='gnss_logger.conversion',operation_id=operation,
        started_utc=utc_now(),gnss_logger_version=__version__,status='FAILED',dry_run=args.dry_run,
        config_path=str(args.config.resolve() if args.config else default_root()/'conversion.local.toml'),
        converter={},selected_inputs=[],segments=[],errors=[],warnings=[],boundaries=[])
    code=1
    try:
        settings=load(args.config)
        root=(args.output or settings.output_root).resolve()/('conversion-'+operation)
        report['output_directory']=str(root)
        report['settings']={key:str(value) if isinstance(value,Path) else value
                            for key,value in asdict(settings).items()}
        report['settings']['output_root']=str(root)
        sources=select(settings,args.input,args.run_id) if sources is None else sources
        report['selected_inputs']=[str(source.path) for source in sources]
        report['segments']=[dict(source={**asdict(source),'path':str(source.path)},
                                 status='NOT_RUN',validation={},warnings=[],errors=[])
                            for source in sources]
        if args.dry_run:
            probe(settings,report['converter'])
        if not args.dry_run:
            root.mkdir(parents=True,exist_ok=True)
        for index,(source,segment) in enumerate(zip(sources,report['segments'])):
            try:
                segment_root=root
                if sum(s.path.stem.casefold()==source.path.stem.casefold() for s in sources)>1:
                    segment_root=root/('segment-'+str(index+1))
                    if not args.dry_run:
                        segment_root.mkdir()
                segment.update(stage_report(settings,source,segment_root,report['converter'].get('options')))
                convert(settings,source,segment,args.dry_run,report['converter'])
            except HANDLED as error:
                segment['status']='FAILED'
                segment['errors'].append(str(error))
            except KeyboardInterrupt:
                segment['status']='INTERRUPTED'
                raise
        report['boundaries']=boundaries(report['segments'],settings.interval_s)
        statuses={segment['status'] for segment in report['segments']}-{'SKIPPED'}
        if report['segments'] and not statuses:
            report['status'],code='SKIPPED',0
        elif args.dry_run and statuses and statuses<= {'DRY_RUN','ALREADY_CONVERTED'}:
            report['status'],code='DRY_RUN',0
        elif statuses and statuses<=SUCCESS:
            report['status']='SUCCESS_WITH_WARNINGS' if any(s['warnings'] for s in report['segments']) or report['boundaries'] else 'SUCCESS'
            code=0
        elif statuses<=SUCCESS|{'INCOMPLETE_NAVIGATION'} and 'INCOMPLETE_NAVIGATION' in statuses:
            report['status'],code='INCOMPLETE_NAVIGATION',3
        elif statuses=={'CONFLICT'}:
            report['status']='CONFLICT'
    except HANDLED as error:
        report['errors'].append(str(error))
    except KeyboardInterrupt:
        report['status'],code='INTERRUPTED',130
    report.update(ended_utc=utc_now(),exit_code=code,
                  summary=dict(Counter(segment['status'] for segment in report['segments'])))
    if not args.dry_run:
        try:
            root.mkdir(parents=True,exist_ok=True)
            destination=root/('conversion-'+operation+'.json')
            temporary=destination.with_suffix('.json.tmp')
            report['report_path']=str(destination)
            write_new_json(temporary,report)
            publish_exclusive(temporary,destination)
        except OSError as error:
            report.pop('report_path',None)
            report['errors'].append('Report publication failed: '+str(error))
            report.update(status='FAILED',exit_code=1)
    return report


def run(args):
    report=execute(args)
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    return report['exit_code']
