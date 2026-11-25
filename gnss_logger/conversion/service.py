"""Application conversion orchestration and per-operation JSON reports."""
from dataclasses import asdict, dataclass, replace
from typing import Optional
from copy import deepcopy
from collections import Counter
from pathlib import Path
import subprocess
import uuid

from .. import __version__
from ..session import utc_now, write_new_json
from ..recording.storage import publish_exclusive
from .config import default_root, load
from .engine import boundaries, check_cancel, convert, probe, stage_report
from .selection import select
from .results import SUCCESS

HANDLED = (OSError, ValueError, subprocess.SubprocessError, KeyError, ImportError)


@dataclass(frozen=True)
class ConversionRequest:
    """CLI-independent inputs for one conversion operation."""

    config: Optional[Path] = None
    output: Optional[Path] = None
    input: Optional[Path] = None
    run_id: Optional[str] = None
    dry_run: bool = False
    allow_obs_without_nav: bool = False

    def __post_init__(self):
        for name in ('config', 'output', 'input'):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Path):
                raise ValueError(name+' must be a Path or None')
        for name in ('dry_run', 'allow_obs_without_nav'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(name+' must be boolean')

    @classmethod
    def from_args(cls, args):
        """Adapt legacy attribute-based callers without a parser dependency."""
        if isinstance(args, cls):
            return args
        return cls(**{name: getattr(args, name, field.default)
                      for name, field in cls.__dataclass_fields__.items()})


def execute(request, sources=None, settings=None, cancel=None, progress=None, *, operation_id=None, invoke_call=None):
    operation=operation_id or uuid.uuid4().hex
    if operation_id is not None and (not isinstance(operation_id,str) or len(operation_id) != 32
            or any(c not in '0123456789abcdef' for c in operation_id)):
        raise ValueError('operation_id must be a lowercase UUID hex string')
    root=(request.output or default_root()/'data/rinex').resolve()/('conversion-'+operation)
    report=dict(schema_version=2,schema_id='gnss_logger.conversion',operation_id=operation,
        started_utc=utc_now(),gnss_logger_version=__version__,status='FAILED',dry_run=request.dry_run,
        config_path=str(request.config.resolve() if request.config else default_root()/'conversion.local.toml'),
        converter={},selected_inputs=[],segments=[],errors=[],warnings=[],boundaries=[])
    code=1
    try:
        settings=load(request.config) if settings is None else replace(settings)
        if request.allow_obs_without_nav:
            if not (settings.obs and settings.nav):
                raise ValueError('--allow-obs-without-nav requires OBS and NAV enabled')
            settings=replace(settings,allow_obs_without_nav=True)
        root=(request.output or settings.output_root).resolve()/('conversion-'+operation)
        report['output_directory']=str(root)
        report['settings']={key:str(value) if isinstance(value,Path) else value
                            for key,value in asdict(settings).items()}
        report['settings']['output_root']=str(root)
        check_cancel(cancel)
        sources=select(settings,request.input,request.run_id) if sources is None else sources
        report['selected_inputs']=[str(source.path) for source in sources]
        report['segments']=[dict(source={**asdict(source),'path':str(source.path)},
                                 status='NOT_RUN',validation={},warnings=[],errors=[])
                            for source in sources]
        if request.dry_run:
            probe(settings,report['converter'], **({'cancel':cancel} if cancel is not None else {}),
                  **({'invoke_call':invoke_call} if invoke_call is not None else {}))
        if not request.dry_run:
            root.mkdir(parents=True,exist_ok=True)
        for index,(source,segment) in enumerate(zip(sources,report['segments'])):
            check_cancel(cancel)
            try:
                segment_root=root
                if sum(s.path.stem.casefold()==source.path.stem.casefold() for s in sources)>1:
                    segment_root=root/('segment-'+str(index+1))
                    if not request.dry_run:
                        segment_root.mkdir()
                segment.update(stage_report(settings,source,segment_root,report['converter'].get('options')))
                convert(settings,source,segment,request.dry_run,report['converter'],
                        **({'cancel':cancel} if cancel is not None else {}),
                        **({'invoke_call':invoke_call} if invoke_call is not None else {}))
            except HANDLED as error:
                segment['status']='FAILED'
                segment['errors'].append(str(error))
            except KeyboardInterrupt:
                segment['status']='INTERRUPTED'
                raise
            if progress is not None:
                progress(deepcopy(report))
        check_cancel(cancel)
        report['boundaries']=boundaries(report['segments'],settings.interval_s)
        statuses={segment['status'] for segment in report['segments']}-{'SKIPPED'}
        if report['segments'] and not statuses:
            report['status'],code='SKIPPED',0
        elif request.dry_run and statuses and statuses<= {'DRY_RUN','ALREADY_CONVERTED'}:
            report['status'],code='DRY_RUN',0
        elif statuses and statuses<=SUCCESS:
            report['status']='SUCCESS_WITH_WARNINGS' if any(s['warnings'] for s in report['segments']) or report['boundaries'] else 'SUCCESS'
            code=0
        elif statuses<=SUCCESS|{'OBS_ONLY_INCOMPLETE_NAVIGATION'} and 'OBS_ONLY_INCOMPLETE_NAVIGATION' in statuses:
            report['status'],code='OBS_ONLY_INCOMPLETE_NAVIGATION',3
        elif statuses<=SUCCESS|{'INCOMPLETE_NAVIGATION','OBS_ONLY_INCOMPLETE_NAVIGATION'} and 'INCOMPLETE_NAVIGATION' in statuses:
            report['status'],code='INCOMPLETE_NAVIGATION',3
        elif statuses=={'CONFLICT'}:
            report['status']='CONFLICT'
    except HANDLED as error:
        report['errors'].append(str(error))
    except KeyboardInterrupt:
        report['status'],code='INTERRUPTED',130
    report.update(ended_utc=utc_now(),exit_code=code,
                  summary=dict(Counter(segment['status'] for segment in report['segments'])))
    if not request.dry_run:
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
