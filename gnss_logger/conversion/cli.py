"""Command-line adapter for conversion operations."""
import json
from pathlib import Path

from . import service
from .service import ConversionRequest


def add_command(sub):
    command=sub.add_parser('convert',help='Convert completed UBX segments to RINEX 3.02')
    source=command.add_mutually_exclusive_group()
    source.add_argument('--input',type=Path,help='Completed UBX file or directory of UBX files')
    source.add_argument('--run-id',help='Completed recording run UUID')
    command.add_argument('--config',type=Path,help='TOML settings')
    command.add_argument('--output',type=Path,help='Root for unique per-operation OBS/NAV and report directories')
    command.add_argument('--dry-run',action='store_true',help='Show arguments and destinations without publishing files')
    command.add_argument('--allow-obs-without-nav',action='store_true',
                         help='Publish validated OBS when requested NAV is missing; incomplete status and exit code 3')


def execute(args, sources=None, *, settings=None, cancel=None, progress=None):
    return service.execute(ConversionRequest.from_args(args), sources,
                           settings=settings, cancel=cancel, progress=progress)


def run(args):
    report=execute(args)
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    return report['exit_code']
