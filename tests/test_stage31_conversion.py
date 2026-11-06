"""Controlled subprocess fixtures for Stage 3.1. No installed RTKLIB required."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from gnss_logger.cli import main
from gnss_logger.mock import synthetic_epoch, synthetic_nav
from gnss_logger.device.ubx import packet
from gnss_logger.conversion.config import load
from gnss_logger.conversion.selection import select
from gnss_logger.conversion.engine import invoke
import test_stage23_projection as stage23
from test_stage23_time import START


def header(kind, version='3.02', system='G'):
    return f'{version:>9}{"":11}{kind:20}{system:20}RINEX VERSION / TYPE\n'


def obs_text(version='3.02', second=0):
    return (header('O',version) +
            f'{"G    4 C1C L1C D1C S1C":60}SYS / # / OBS TYPES\n' +
            f'{"":48}GPS{"":9}TIME OF FIRST OBS\n' +
            f'{"":60}END OF HEADER\n' +
            f'> 2026 10 28 23 59 {second:010.7f}  0  1\n' +
            'G01'+''.join(f'{value:14.3f}  ' for value in (23000000,12000000,1500,45))+'\n')


def nav_text(systems='G'):
    text=header('N',system=systems if len(systems)==1 else 'M')+f'{"":60}END OF HEADER\n'
    for system in systems:
        text+=system+'01 2026 10 28 00 00 00'+f'{1:19.12E}'*3+'\n'
        text+=('    '+f'{1:19.12E}'*4+'\n')*(3 if system=='R' else 7)
    return text


class ConversionTests(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='conversion with spaces ')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.exe=self.root/'convbin.exe'
        self.exe.write_bytes(b'controlled subprocess fixture')
        self.config=self.root/'conversion.toml'
        self.config.write_text('[engine]\nexecutable="convbin.exe"\n[input]\nruns_root="internal/runs"\n'
                               'daily_root="data"\n[output]\nroot="rinex"\n',encoding='utf-8')
        self.output=self.root/'rinex'
        self.source=self.root/'segment one.ubx'
        self.original=synthetic_epoch(START)+synthetic_nav()
        self.source.write_bytes(self.original)
        self.calls=[]
        self.help='usage: convbin\n'+'\n'.join(
            '     '+key+'  option' for key in ('-r','-v','-f','-mask','-y','-o','-n','-od','-os','-hr'))+'\n ubx\n'
        self.obs=obs_text()
        self.nav=nav_text()
        self.returncode=0
        self.aux=None
        self.side_effect=None

    def subprocess_fixture(self,args,**kwargs):
        self.assertFalse(kwargs['shell'])
        self.calls.append(args)
        cwd=Path(kwargs['cwd'])
        if len(args)==1 or args[-1]=='--help':
            kwargs['stderr'].write(self.help.encode())
            return subprocess.CompletedProcess(args,1)
        if args[-1]=='--version':
            kwargs['stderr'].write(b'convbin RTKLIB EX 2.5.1\n')
            return subprocess.CompletedProcess(args,0)
        self.assertEqual(args[0],str(self.exe))
        self.assertEqual(args[-1],'input.ubx')
        self.assertTrue((cwd/'input.ubx').read_bytes().startswith(self.original[:2]))
        if self.side_effect:
            self.side_effect()
        if self.obs is not None and '-o' in args:
            (cwd/'output.obs').write_text(self.obs,encoding='ascii')
        if self.nav is not None and '-n' in args:
            (cwd/'output.nav').write_text(self.nav,encoding='ascii')
        if self.aux:
            (cwd/self.aux).write_bytes(b'auxiliary fixture')
        kwargs['stderr'].write(b'conversion fixture output')
        return subprocess.CompletedProcess(args,self.returncode)

    def run_cli(self,*extra,explicit=True):
        args=['convert','--config',str(self.config)]
        if explicit:
            args+=['--input',str(self.source)]
        with patch('gnss_logger.conversion.engine.subprocess.run',self.subprocess_fixture):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                code=main(args+list(extra))
        report=json.loads(output.getvalue())
        self.assertEqual(code,report['exit_code'])
        if 'report_path' in report:
            self.assertEqual(json.loads(Path(report['report_path']).read_text(encoding='utf-8')),report)
        return report

    def test_default_settings_and_success_report(self):
        settings=load(self.config)
        self.assertEqual(settings.version,'3.02')
        self.assertIn('EL7Q',settings.masks)
        self.assertIn('EL6X',settings.masks)
        report=self.run_cli()
        self.assertEqual(report['status'],'SUCCESS_WITH_WARNINGS')
        segment=report['segments'][0]
        self.assertEqual(segment['process']['returncode'],0)
        self.assertEqual(segment['validation']['obs']['epochs'],1)
        self.assertEqual(segment['validation']['nav']['records'],{'G':1})
        self.assertEqual(segment['source']['sha256'],hashlib.sha256(self.original).hexdigest())
        self.assertEqual(report['converter']['sha256'],hashlib.sha256(self.exe.read_bytes()).hexdigest())
        self.assertEqual(report['converter']['version'],'convbin RTKLIB EX 2.5.1')
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual({p.suffix for p in self.output.iterdir()},{'.obs','.nav','.json'})
        args=segment['args']
        self.assertEqual(args[args.index('-f')+1],'4')
        self.assertEqual([args[i+1] for i,arg in enumerate(args) if arg=='-y'],list('JSI'))

    def test_exact_mask_and_nav_only_configuration(self):
        self.config.write_text('[engine]\nexecutable="convbin.exe"\n[output]\nroot="rinex"\nobs=false\n'
                               '[rinex]\nsystems=["G"]\nsignal_groups=["L1"]\nsignal_masks=["GL1C"]\n')
        report=self.run_cli()
        self.assertEqual(report['status'],'SUCCESS')
        self.assertEqual(set(report['segments'][0]['outputs']),{'nav'})
        self.assertFalse(list(self.output.glob('*.obs')))

    def test_invalid_config_rejects_new_version_unknown_field_and_bad_masks(self):
        for text in ('[rinex]\nversion="3.04"', '[engine]\nunknown=true',
                     '[rinex]\nsignal_masks=["CL5D"]', '[rinex]\nobservations=["L"]'):
            with self.subTest(text=text):
                self.config.write_text(text)
                self.assertEqual(self.run_cli('--dry-run')['status'],'FAILED')
        self.assertFalse(self.calls)

    def test_missing_executable(self):
        self.exe.unlink()
        report=self.run_cli()
        self.assertIn('not found',report['errors'][0])
        self.assertEqual(self.calls,[])

    def test_unsupported_options_and_version_fallback(self):
        self.help=self.help.replace('     -mask  option\n','')
        report=self.run_cli()
        self.assertIn('-mask',report['errors'][0])
        self.assertFalse(list(self.output.glob('*.obs')))

    def test_help_without_version_support(self):
        fixture=self.subprocess_fixture
        def old(args,**kw):
            if args[-1]=='--version':
                kw['stderr'].write(self.help.encode())
                return subprocess.CompletedProcess(args,1)
            return fixture(args,**kw)
        self.subprocess_fixture=old
        self.assertIsNone(self.run_cli('--dry-run')['converter']['version'])

    def test_explicit_directory_multiple_segments_and_partial_exclusion(self):
        (self.root/'other.ubx').write_bytes(self.original)
        (self.root/'active.ubx.part').write_bytes(self.original)
        report=self.run_cli('--input',str(self.root),explicit=False)
        self.assertEqual(len(report['segments']),2)
        self.assertFalse(any('.part' in path for path in report['selected_inputs']))
        self.assertEqual(len(report['boundaries']),1)
        self.assertEqual(len(list(self.output.glob('*.obs'))),2)
        self.assertEqual(self.run_cli('--input',str(self.root/'active.ubx.part'),explicit=False)['status'],'FAILED')

    def test_dry_run_prints_command_without_output_directory(self):
        report=self.run_cli('--dry-run')
        self.assertEqual(report['status'],'DRY_RUN')
        self.assertTrue(report['segments'][0]['args'])
        self.assertFalse(self.output.exists())
        self.assertTrue(all(len(args)<=2 for args in self.calls))

    def test_existing_output_preserved_and_retry_in_new_root(self):
        self.output.mkdir()
        collision=self.output/'segment one.obs'
        collision.write_bytes(b'existing observation')
        self.assertEqual(self.run_cli()['status'],'FAILED')
        self.assertEqual(collision.read_bytes(),b'existing observation')
        self.assertFalse(list(self.output.glob('*.nav')))
        result=self.run_cli('--output',str(self.root/'new output'))
        self.assertIn(result['status'],('SUCCESS','SUCCESS_WITH_WARNINGS'))

    def test_nonzero_process_and_interruption_leave_no_outputs(self):
        self.returncode=4
        self.assertEqual(self.run_cli()['segments'][0]['process']['returncode'],4)
        self.returncode=0
        def interrupted():
            raise KeyboardInterrupt
        self.side_effect=interrupted
        self.assertEqual(self.run_cli()['status'],'INTERRUPTED')
        self.assertFalse(list(self.output.glob('*.obs')))
        self.assertFalse(list(self.output.glob('.convbin-*')))

    def test_timeout_preserves_captured_log_without_outputs(self):
        def timed_out():
            raise subprocess.TimeoutExpired('controlled fixture',1)
        self.side_effect=timed_out
        report=self.run_cli()
        self.assertEqual(report['status'],'FAILED')
        self.assertTrue(report['segments'][0]['process']['timed_out'])
        self.assertFalse(list(self.output.glob('*.obs')))

    def test_native_subprocess_receives_space_containing_arguments(self):
        argument='input with spaces.ubx'
        result=invoke([sys.executable,'-c','import sys; print(sys.argv[1])',argument],self.root,10)
        self.assertEqual(result['returncode'],0)
        self.assertEqual(result['stdout'].strip(),argument)

    def test_invalid_outputs(self):
        for obs in (None,'',obs_text('3.04'),header('O')+f'{"":60}END OF HEADER\n'):
            with self.subTest(obs=obs):
                self.obs=obs
                self.assertEqual(self.run_cli()['status'],'FAILED')
        self.obs=obs_text()
        for nav in (None,'',nav_text()[:-80]):
            with self.subTest(nav=nav):
                self.nav=nav
                self.assertEqual(self.run_cli()['status'],'INCOMPLETE_NAVIGATION')
        self.assertFalse(list(self.output.glob('*.obs')))
        self.assertFalse(list(self.output.glob('*.nav')))

    def test_mixed_nav_coverage_and_separate_nav_refusal(self):
        self.source.write_bytes(self.original+b''.join(packet(2,0x13,bytes([gnss,1,0,0,1,0,2,0])+bytes(4))
                                                      for gnss in (2,3,6)))
        report=self.run_cli()
        self.assertEqual(report['status'],'INCOMPLETE_NAVIGATION')
        self.nav=nav_text('GREC')
        self.aux='input.gnav'
        self.assertEqual(self.run_cli()['status'],'INCOMPLETE_NAVIGATION')
        self.aux=None
        report=self.run_cli()
        self.assertEqual(report['segments'][0]['validation']['nav']['records'],dict.fromkeys('GREC',1))

    def test_publish_race_rolls_back_only_new_obs(self):
        collision=self.output/'segment one.nav'
        self.side_effect=lambda: collision.write_bytes(b'existing nav from concurrent conversion')
        report=self.run_cli()
        self.assertEqual(report['status'],'FAILED')
        self.assertFalse((self.output/'segment one.obs').exists())
        self.assertEqual(collision.read_bytes(),b'existing nav from concurrent conversion')

    def test_latest_and_explicit_run_use_authoritative_manifest(self):
        self.midnight=stage23.ProjectionTests.midnight.__get__(self)
        first=stage23.ProjectionTests.run_record(self,root=self.root)[0]
        second=stage23.ProjectionTests.run_record(self,root=self.root)[0]
        for controller,day in ((first,'28'),(second,'29')):
            path=controller.directory/'manifest.json'
            record=json.loads(path.read_text(encoding='utf-8'))
            record['ended_utc']='2099-10-'+day+'T12:00:00+00:00'
            path.write_text(json.dumps(record),encoding='utf-8')
        first_path=first.directory/'manifest.json'
        import os
        os.utime(first_path,(2000000000,2000000000))
        settings=load(self.config)
        latest=select(settings)
        self.assertEqual({s.run_id for s in latest},{second.run_id})
        self.assertEqual(len(latest),3)
        self.assertTrue(any(s.utc_day is None for s in latest))
        report=self.run_cli('--run-id',first.run_id,'--dry-run',explicit=False)
        self.assertEqual({s['source']['run_id'] for s in report['segments']},{first.run_id})
        self.assertEqual(select(settings,input_path=self.source)[0].path,self.source)
        self.assertEqual(self.run_cli('--run-id','../escape','--dry-run',explicit=False)['status'],'FAILED')
        # Explicit run conversion refuses content that no longer matches its manifest.
        latest[0].path.write_bytes(self.original)
        with patch('gnss_logger.conversion.engine.subprocess.run',self.subprocess_fixture):
            result=self.run_cli('--run-id',second.run_id,explicit=False)
        self.assertTrue(any('fingerprint' in error for segment in result['segments'] for error in segment['errors']))
