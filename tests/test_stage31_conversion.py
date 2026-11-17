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
        self.assertEqual({p.suffix for p in Path(report['output_directory']).iterdir()},{'.obs','.nav','.json'})
        args=segment['args']
        self.assertEqual(args[args.index('-f')+1],'4')
        self.assertEqual([args[i+1] for i,arg in enumerate(args) if arg=='-y'],list('JSI'))

    def test_undated_control_only_is_skipped_without_invoking_converter(self):
        self.source.write_bytes(packet(10,4,b'control fixture'))
        self.exe.unlink()  # Even help/version probes are unnecessary.
        report=self.run_cli()
        self.assertEqual(report['status'],'SKIPPED')
        self.assertEqual(report['exit_code'],0)
        segment=report['segments'][0]
        self.assertEqual(segment['skip_reason'],'no_rawx_undated_control_only')
        self.assertEqual(segment['source']['rawx_epochs'],0)
        self.assertEqual(segment['outputs'],{})
        self.assertEqual(segment['errors'],[])
        self.assertFalse(self.calls)
        self.assertFalse(list(self.output.rglob('*.obs')))
        self.assertFalse(list(self.output.rglob('*.nav')))

    def test_mixed_control_and_rawx_session_succeeds(self):
        (self.root/'control.ubx').write_bytes(packet(10,4,b'control fixture'))
        report=self.run_cli('--input',str(self.root),explicit=False)
        self.assertEqual(report['status'],'SUCCESS_WITH_WARNINGS')
        self.assertEqual(report['summary'],{'SKIPPED':1,'SUCCESS_WITH_WARNINGS':1})
        self.assertEqual(len([a for a in self.calls if a[-1]=='input.ubx']),1)
        self.assertEqual(len(list(self.output.rglob('*.obs'))),1)

    def test_parallel_operations_and_same_stem_segments_are_isolated(self):
        from argparse import Namespace
        from concurrent.futures import ThreadPoolExecutor
        from gnss_logger.conversion.cli import execute
        from gnss_logger.conversion.selection import Source
        other=self.root/'other'
        other.mkdir()
        duplicate=other/self.source.name
        duplicate.write_bytes(self.original)
        sources=[Source(self.source),Source(duplicate)]
        args=Namespace(config=self.config,output=None,dry_run=False)
        with patch('gnss_logger.conversion.engine.subprocess.run',self.subprocess_fixture):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports=list(pool.map(lambda _:execute(args,sources=sources),range(2)))
        self.assertNotEqual(reports[0]['output_directory'],reports[1]['output_directory'])
        for report in reports:
            self.assertEqual(report['summary'],{'SUCCESS_WITH_WARNINGS':2})
            outputs=[segment['published_outputs']['obs'] for segment in report['segments']]
            self.assertEqual(len(set(outputs)),2)
            self.assertTrue(all(Path(path).is_file() for path in outputs))

    def test_skip_does_not_mask_convbin_error(self):
        (self.root/'control.ubx').write_bytes(packet(10,4,b'control'))
        self.returncode=4
        report=self.run_cli('--input',str(self.root),explicit=False)
        self.assertEqual(report['status'],'FAILED')
        self.assertEqual(report['exit_code'],1)
        self.assertEqual(report['summary'],{'SKIPPED':1,'FAILED':1})
        failed=next(s for s in report['segments'] if s['status']=='FAILED')
        self.assertEqual(failed['process']['returncode'],4)
        self.assertTrue(failed['errors'])
        self.assertFalse(list(self.output.rglob('*.obs')))

    def test_skipped_segment_does_not_hide_boundary_gap(self):
        from gnss_logger.conversion.engine import boundaries
        def segment(name,second):
            return dict(status='SUCCESS',source={'path':name},validation={'obs':dict(
                first_epoch=f'2026-11-16T00:00:{second:02}',
                last_epoch=f'2026-11-16T00:00:{second:02}',time_system='GPS')})
        results=boundaries([segment('before',0),dict(status='SKIPPED'),segment('after',5)],1)
        self.assertEqual(results[0]['seconds'],5)

    def test_existing_result_verification_inside_operation_is_preserved(self):
        from gnss_logger.conversion.engine import convert,stage_report
        from gnss_logger.conversion.selection import Source
        report=self.run_cli()
        settings=load(self.config)
        source=Source(self.source)
        root=Path(report['output_directory'])
        segment=stage_report(settings,source,root,report['converter']['options'])
        convert(settings,source,segment,False,report['converter'])
        self.assertEqual(segment['status'],'ALREADY_CONVERTED')
        Path(segment['published_outputs']['nav']).write_bytes(b'tampered')
        segment=stage_report(settings,source,root,report['converter']['options'])
        convert(settings,source,segment,False,report['converter'])
        self.assertEqual(segment['status'],'CONFLICT')

    def test_corrupt_control_is_not_skipped(self):
        for contents in (packet(10,4,b'control')+b'noise',packet(10,4,b'control')[:-1],
                         packet(2,0x15,b'invalid rawx')):
            self.source.write_bytes(contents)
            report=self.run_cli()
            self.assertEqual(report['summary'],{'FAILED':1})
            self.assertNotIn('skip_reason',report['segments'][0])
        self.assertFalse(self.calls)

    def test_all_control_segments_are_skipped_without_rinex(self):
        self.source.write_bytes(packet(10,4,b'control'))
        (self.root/'second.ubx').write_bytes(packet(10,4,b'control two'))
        report=self.run_cli('--input',str(self.root),explicit=False)
        self.assertEqual(report['status'],'SKIPPED')
        self.assertEqual(report['summary'],{'SKIPPED':2})
        self.assertEqual(report['exit_code'],0)
        self.assertFalse(self.calls)
        self.assertFalse(list(self.output.rglob('*.obs')))
        self.assertFalse(list(self.output.rglob('*.nav')))

    def test_dated_no_rawx_and_unusable_rawx_keep_failure(self):
        from argparse import Namespace
        from gnss_logger.conversion.cli import execute
        from gnss_logger.conversion.selection import Source
        self.source.write_bytes(packet(10,4,b'control'))
        args=Namespace(config=self.config,output=None,dry_run=False)
        report=execute(args,sources=[Source(self.source,utc_day='2026-11-16')])
        self.assertEqual(report['summary'],{'FAILED':1})
        self.source.write_bytes(synthetic_epoch(START,signal_ids=()))
        self.assertEqual(self.run_cli()['summary'],{'FAILED':1})

    def test_control_fingerprint_mismatch_is_not_skipped(self):
        from argparse import Namespace
        from gnss_logger.conversion.cli import execute
        from gnss_logger.conversion.selection import Source
        self.source.write_bytes(packet(10,4,b'control'))
        report=execute(Namespace(config=self.config,output=None,dry_run=False),
                       sources=[Source(self.source,size=1,sha256='0'*64)])
        self.assertEqual(report['summary'],{'FAILED':1})
        self.assertIn('fingerprint',report['segments'][0]['errors'][0])

    def test_signal_warnings_keep_success_with_warnings(self):
        import struct
        payload=bytearray(synthetic_epoch(START,signal_ids=(0,0,0,0))[6:-2])
        for index,(gnss,sig) in enumerate(((3,4),(2,8),(3,7),(3,5))):
            struct.pack_into('BB',payload,16+32*index+20,gnss,3)
            payload[16+32*index+22]=sig
        self.source.write_bytes(self.original+packet(2,0x15,payload))
        report=self.run_cli()
        segment=report['segments'][0]
        self.assertEqual(report['status'],'SUCCESS_WITH_WARNINGS')
        self.assertEqual(segment['errors'],[])
        self.assertTrue(any(w.startswith('Requested source signals missing in OBS: C6I, E6B/E6X')
                            for w in segment['warnings']))
        self.assertIn('Source RAWX signals outside the supported RINEX 3.02 profile: 3:7, 3:5',segment['warnings'])

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
        self.assertIn('not found',report['segments'][0]['errors'][0])
        self.assertEqual(self.calls,[])

    def test_unsupported_options_and_version_fallback(self):
        self.help=self.help.replace('     -mask  option\n','')
        report=self.run_cli()
        self.assertIn('-mask',report['segments'][0]['errors'][0])
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
        self.assertEqual(len(list(self.output.rglob('*.obs'))),2)
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
        self.assertEqual(self.run_cli()['status'],'SUCCESS_WITH_WARNINGS')
        self.assertEqual(collision.read_bytes(),b'existing observation')
        self.assertFalse(list(self.output.glob('*.nav')))
        result=self.run_cli('--output',str(self.root/'new output'))
        self.assertIn(result['status'],('SUCCESS','SUCCESS_WITH_WARNINGS'))

    def test_eight_segments_repeat_and_detect_tampering(self):
        inputs = self.root/'batch'
        inputs.mkdir()
        for index in range(8):
            (inputs/f'segment{index}.ubx').write_bytes(self.original)
        args = ('--input',str(inputs))
        first = self.run_cli(*args,explicit=False)
        self.assertEqual(first['summary'],{'SUCCESS_WITH_WARNINGS':8})
        self.calls.clear()
        second = self.run_cli(*args,explicit=False)
        self.assertEqual(second['summary'],{'SUCCESS_WITH_WARNINGS':8})
        self.assertNotEqual(first['output_directory'],second['output_directory'])
        original_nav=Path(first['output_directory'])/'segment0.nav'
        original_nav.write_bytes(b'foreign NAV')
        third = self.run_cli(*args,explicit=False)
        self.assertEqual(third['summary'],{'SUCCESS_WITH_WARNINGS':8})
        self.assertEqual(original_nav.read_bytes(),b'foreign NAV')

    def test_legacy_report_proves_existing_pair(self):
        first = self.run_cli()
        segment = first['segments'][0]
        Path(segment['result_path']).unlink()
        first['schema_version'] = 1
        Path(first['report_path']).write_text(json.dumps(first),encoding='utf-8')
        from gnss_logger.conversion.results import identity, legacy
        found,path=legacy(Path(first['output_directory']),
                          identity(load(self.config),segment,first['converter']),segment['outputs'])
        self.assertIsNotNone(found)
        self.assertEqual(path,first['report_path'])

    def test_corrupt_and_empty_input_do_not_block_next_segment(self):
        inputs = self.root/'batch'
        inputs.mkdir()
        (inputs/'a.ubx').write_bytes(self.original+b'noise')
        (inputs/'b.ubx').touch()
        (inputs/'c.ubx').write_bytes(self.original)
        result = self.run_cli('--input',str(inputs),explicit=False)
        self.assertEqual(result['summary'],{'FAILED':2,'SUCCESS_WITH_WARNINGS':1})
        self.assertEqual(list(self.output.rglob('*.obs')),[Path(result['output_directory'])/'c.obs'])

    def test_receipt_disk_full_rolls_back_outputs(self):
        import errno
        from gnss_logger.conversion.results import publish_exclusive
        def disk_full(source,target):
            if source.name=='result.json':
                raise OSError(errno.ENOSPC,'disk full')
            publish_exclusive(source,target)
        with patch('gnss_logger.conversion.results.publish_exclusive',disk_full):
            result = self.run_cli()
        self.assertEqual(result['summary'],{'FAILED':1})
        self.assertFalse(list(self.output.glob('*.obs')))
        self.assertFalse(list(self.output.glob('*.nav')))
        self.assertFalse(list(self.output.glob('*.conversion.json')))

    def test_missing_receipt_after_interruption_is_conflict(self):
        first = self.run_cli()
        Path(first['segments'][0]['result_path']).unlink()
        result = self.run_cli()
        self.assertEqual(result['summary'],{'SUCCESS_WITH_WARNINGS':1})
        self.assertNotEqual(result['output_directory'],first['output_directory'])

    def record_with_worker(self):
        from test_stage2_record import Clock, ScriptReader
        from gnss_logger.recording.controller import RecordController
        clock=Clock()
        steps=stage23.ProjectionTests.midnight(self)
        def controller(**options):
            return RecordController(reader_factory=lambda settings,conn:ScriptReader(conn,steps,clock),
                                    clock=clock,**options)
        args=['record','--output',str(self.root/'internal/runs'),'--daily-root',str(self.root/'data'),
              '--port','COM_TEST','--model','ZED-F9P','--vid','0x1546','--pid','0x01a9',
              '--usb-location','1-9','--seconds','100','--convert-config',str(self.config)]
        with patch('gnss_logger.recording.controller.RecordController',side_effect=controller), \
                patch('gnss_logger.conversion.engine.subprocess.run',self.subprocess_fixture), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args),0)
        report=json.loads(output.getvalue())
        return report['extensions']['gnss_logger.conversion']

    def test_record_dispatches_only_committed_segments_to_worker(self):
        self.exe.unlink()
        conversion=self.record_with_worker()
        self.assertEqual(conversion['statuses'],{'SKIPPED':1,'FAILED':2})
        self.assertEqual(json.loads(Path(conversion['summary_path']).read_text(encoding='utf-8'))['statuses'],
                         {'SKIPPED':1,'FAILED':2})
        for path in self.output.rglob('conversion-*.json'):
            result=json.loads(path.read_text(encoding='utf-8'))
            self.assertIn(result['summary'],({'SKIPPED':1},{'FAILED':1}))
            self.assertTrue(Path(result['segments'][0]['source']['path']).is_file())
            self.assertTrue(result['segments'][0]['source']['artifact_id'])

    def test_record_worker_publishes_outputs_and_batch_can_reuse_them(self):
        conversion=self.record_with_worker()
        # Initial undated MON-VER segment has no observations.
        self.assertEqual(conversion['statuses'],{'SKIPPED':1,'SUCCESS_WITH_WARNINGS':2})
        self.assertEqual(len(list(self.output.rglob('*.conversion.json'))),2)
        repeated=self.run_cli(explicit=False)
        self.assertEqual(repeated['summary'],{'SKIPPED':1,'SUCCESS_WITH_WARNINGS':2})

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
        collisions=[]
        def collide():
            collision=next(self.output.glob('conversion-*'))/'segment one.nav'
            collision.write_bytes(b'existing nav from concurrent conversion')
            collisions.append(collision)
        self.side_effect=collide
        report=self.run_cli()
        self.assertEqual(report['status'],'CONFLICT')
        self.assertFalse(list(self.output.rglob('*.obs')))
        self.assertEqual(collisions[0].read_bytes(),b'existing nav from concurrent conversion')

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
