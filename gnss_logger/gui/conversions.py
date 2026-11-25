"""Real conversion orchestration with per-instance synthetic converter I/O.

MOCK RINEX is structural fixture data, never scientific measurements. No global
patches, subprocesses, test imports or alternative job/queue implementations.
"""
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from threading import Event
import json
import sys
from uuid import uuid4

from .. import application as api
from ..conversion import service
from ..conversion.config import GROUPS, MASKS
from ..conversion.engine import check_cancel
from ..conversion.selection import select
from ..conversion.worker import SegmentWorker
from ..device.ubx import packet
from ..mock import synthetic_epoch, synthetic_nav


class ProductionConversionBackend:
    is_mock = False

    def load_settings(self, path=None):
        return api.load_conversion_settings(path)

    def create_job(self, request, settings):
        return api.ConversionJob(request, settings=settings)


ConversionBackend = ProductionConversionBackend


class MockConversionBackend(ProductionConversionBackend):
    """Use real jobs, AUTO queues and engine with an isolated synthetic runner.

    The UI sets scenario before creating manual jobs/AUTO workers. Creation
    freezes it; changing scenario cannot change queued or active conversions.
    sys.executable supplies a real fingerprint only and is never launched.
    """
    is_mock = True
    scenarios = ('success', 'warnings', 'obs_only', 'failed', 'slow', 'skipped')

    def __init__(self, mock_root: Path):
        self.mock_root = Path(mock_root).resolve()
        self.scenario = 'success'

    @property
    def scenario(self):
        return self._scenario

    @scenario.setter
    def scenario(self, value):
        if value not in self.scenarios:
            raise ValueError('Unknown MOCK conversion scenario: ' + str(value))
        self._scenario = value

    def _inside(self, path):
        resolved = Path(path).resolve()
        if resolved == self.mock_root or not resolved.is_relative_to(self.mock_root):
            raise ValueError('MOCK path must be beneath mock_root: ' + str(path))
        return resolved

    def _check_settings(self, settings):
        for name in ('runs_root', 'daily_root', 'output_root'):
            self._inside(getattr(settings, name))
        return settings

    def load_settings(self, path=None):
        if path is not None:
            return self._check_settings(api.load_conversion_settings(self._inside(path)))
        return self._check_settings(api.ConversionSettings(
            executable=Path(sys.executable).resolve(),
            runs_root=self._inside(self.mock_root / 'runs'),
            daily_root=self._inside(self.mock_root / 'data'),
            output_root=self._inside(self.mock_root / 'rinex'),
            version='3.02', systems=('G',), observations=tuple('CLDS'),
            groups=(GROUPS[0],), masks=tuple(
                mask for mask, slot in MASKS.items() if mask[0] == 'G' and slot == 1),
            obs=True, nav=True, receiver_model='MOCK', timeout_s=10,
            interval_s=1, allow_obs_without_nav=False))

    def make_fixture(self, scenario='success'):
        self.scenario = scenario
        path = self._inside(self.mock_root / 'fixtures' /
                            ('MOCK-' + scenario + '-' + uuid4().hex + '.ubx'))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._inside(path)
        data = (packet(10, 4, b'MOCK control fixture') if scenario == 'skipped' else
                synthetic_epoch(datetime(2026, 10, 28, tzinfo=timezone.utc),
                                signal_ids=(0,)) + synthetic_nav())
        with path.open('xb') as stream:
            stream.write(data)
        return path

    def create_job(self, request, settings):
        return api.ConversionJob(request, settings=self._check_settings(settings),
                                 execute_call=partial(self.execute, scenario=self.scenario))

    def worker_factory(self, config=None, receiver_model=None, settings=None):
        if config is not None:
            self._inside(config)
        settings = self.load_settings(config) if settings is None else self._check_settings(settings)
        return SegmentWorker(config, receiver_model=receiver_model, settings=settings,
                             execute_call=partial(self.execute, scenario=self.scenario))

    def prepare_recording(self, provider):
        provider.conversion_worker_factory = self.worker_factory
        return provider

    def execute(self, request, sources=None, settings=None, cancel=None,
                progress=None, operation_id=None, *, scenario=None):
        scenario = self.scenario if scenario is None else scenario
        if scenario not in self.scenarios:
            raise ValueError('Unknown MOCK conversion scenario: ' + str(scenario))
        request = api.ConversionRequest.from_args(request)
        if request.config is not None:
            self._inside(request.config)
        settings = self.load_settings(request.config) if settings is None else replace(settings)
        self._check_settings(settings)
        if scenario == 'warnings':
            # Request a supported group absent from the synthetic source. The
            # real engine derives the warning; OBS/NAV remain structurally valid.
            settings = replace(settings, groups=tuple(dict.fromkeys(
                settings.groups + (GROUPS[1], GROUPS[2]))))
        if request.input is not None:
            self._inside(request.input)
        self._inside(request.output or settings.output_root)
        # Resolve selection here so every source (including AUTO artifacts and
        # directory members) is checked before the engine opens it.
        sources = tuple(select(settings, request.input, request.run_id)
                        if sources is None else sources)
        for source in sources:
            self._inside(source.path)
        invoke = partial(self.invoke, scenario=scenario)
        def mark(report):
            report.update(is_mock=True, simulated=True, mock_scenario=scenario)
            warning = 'MOCK: synthetic fixture data; not scientific measurements or ephemeris'
            if warning not in report.setdefault('warnings', []):
                report['warnings'].append(warning)
            return report

        def publish(report):
            if progress is not None:
                progress(mark(report))

        report = mark(service.execute(
            request, sources, settings=settings, cancel=cancel,
            progress=publish, operation_id=operation_id, invoke_call=invoke))
        # Annotate the real service's persisted report too, without changing
        # wire statuses or segment receipts. Replace only our confined report.
        if report.get('report_path'):
            path = self._inside(report['report_path'])
            temporary = self._inside(path.with_suffix('.mock-' + uuid4().hex + '.tmp'))
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(report, stream, indent=2)
                self._inside(path)
                temporary.replace(path)
            finally:
                if temporary.exists():
                    self._inside(temporary).unlink()
        return report

    def invoke(self, args, cwd, timeout, cancel=None, *, scenario=None):
        """Synthetic invoke protocol; probe temp directories are never written.

        Real probe owns its external temporary directory. Only conversion work
        beneath mock_root may receive synthetic outputs.
        """
        scenario = self.scenario if scenario is None else scenario
        check_cancel(cancel)
        result = dict(returncode=0, timed_out=False, stdout='', stderr='',
                      stdout_truncated=False, stderr_truncated=False)
        if len(args) == 1 or args[-1] == '--help':
            result['stdout'] = ('MOCK synthetic convbin interface; ubx\n' +
                '\n'.join('  ' + option + ' option' for option in
                    ('-r', '-v', '-f', '-mask', '-y', '-o', '-n', '-od', '-os', '-hr')))
            return result
        if args[-1] == '--version':
            result['stdout'] = 'MOCK RTKLIB synthetic fixture runner'
            return result
        work = self._inside(cwd)
        self._inside(work / 'input.ubx')
        if args[-1] != 'input.ubx' or not (work / 'input.ubx').is_file():
            raise ValueError('MOCK runner expects an engine-owned input snapshot')
        delay = 3.0 if scenario == 'slow' else 0.15
        event = cancel if cancel is not None else Event()
        event.wait(min(delay, timeout))
        check_cancel(cancel)
        if timeout < delay:
            result.update(returncode=None, timed_out=True)
            return result
        if scenario == 'failed':
            result.update(returncode=1, stderr='MOCK synthetic converter failure')
            return result
        for option, kind, text in (('-o', 'obs', _mock_obs_text),
                                   ('-n', 'nav', _mock_nav_text)):
            if option not in args or (kind == 'nav' and scenario == 'obs_only'):
                continue
            target = self._inside(work / args[args.index(option) + 1])
            if target.parent != work or target.name != 'output.' + kind:
                raise ValueError('Unexpected MOCK converter output path')
            check_cancel(cancel)
            with target.open('x', encoding='ascii') as stream:
                stream.write(text())
        result['stderr'] = 'MOCK synthetic fixture output; not scientific data'
        return result


def _rinex_header(kind):
    return f'{"3.02":>9}{"":11}{kind:20}{"G":20}RINEX VERSION / TYPE\n'


def _mock_obs_text():
    # Local version of the Stage 3.1 structural fixture; no test runtime import.
    return (_rinex_header('O') +
            f'{"MOCK SYNTHETIC - NOT MEASUREMENTS":60}COMMENT\n' +
            f'{"G    4 C1C L1C D1C S1C":60}SYS / # / OBS TYPES\n' +
            f'{"":48}GPS{"":9}TIME OF FIRST OBS\n' +
            f'{"":60}END OF HEADER\n' +
            '> 2026 10 28 23 59 00.0000000  0  1\n' +
            'G01' + ''.join(f'{value:14.3f}  ' for value in
                            (23000000, 12000000, 1500, 45)) + '\n')


def _mock_nav_text():
    return (_rinex_header('N') +
            f'{"MOCK SYNTHETIC - NOT EPHEMERIS":60}COMMENT\n' +
            f'{"":60}END OF HEADER\n' +
            'G01 2026 10 28 00 00 00' + f'{1:19.12E}' * 3 + '\n' +
            ('    ' + f'{1:19.12E}' * 4 + '\n') * 7)
