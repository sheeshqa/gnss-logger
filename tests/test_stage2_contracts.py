"""Contract acceptance, independent invalid examples, and Stage 1 CLI isolation."""

import contextlib
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from gnss_logger.cli import main, parser
from gnss_logger.recording.contracts import (ByteAccounting, DeviceBinding, InputChunk,
    RawRecord, RecordSettings, State, TimeBinding, WriteReceipt, require_transition, require_unique_match)
from gnss_logger.recording.schema import get_schema, validate_record


RUN, ATTEMPT, CONNECTION, PRIMARY, DAILY = ('1'*32, '2'*32, '3'*32, '4'*32, '5'*32)
STAMP = '2026-10-22T23:59:59Z'
HASH = hashlib.sha256(b'abc').hexdigest()


def ledger(**changes):
    value = dict(received=3, journal_written=3, journal_synced=3, pending=0, unarchived=0,
                 framed=3, opaque=0, unclassified=0, received_count_exact=True)
    value.update(changes)
    return value


def binding():
    return DeviceBinding('ZED-F9P', 0x1546, 0x01A9, usb_location='1-9')


def envelope(kind):
    return dict(schema_id='gnss_logger.stage2.'+kind, schema_version=1,
                contract_version='stage2.1/v1', run_id=RUN, attempt_id=ATTEMPT)


def manifest():
    return dict(envelope('manifest'), state='STOPPED', started_utc=STAMP,
        ended_utc='2026-10-23T00:00:01Z', elapsed_monotonic_ns=2_000_000_000, clock_discontinuities=0,
        read_only=True, automatic_delete=False, retention_policy='keep_all',
        hardware_validation='NOT TESTED', binding=asdict(binding()), accounting=ledger(),
        parameters=dict(seconds=2,until_stopped=False,baud=115200,expected_hz=1,station='UBX1'),
        output_directory='C:/record', versions=dict(gnss_logger='0.3.0',python='3.12',platform='Windows',dependencies={'pyserial':'3.5'}),
        connections=[dict(connection_id=CONNECTION,port='COM10',opened_utc=STAMP,closed_utc='2026-10-23T00:00:01Z',
            identity_status='MATCHED',match_strength='USB_TOPOLOGY',observed_model='ZED-F9P',firmware='HPG 1.13',protocol='27.12',
            vid=0x1546,pid=0x01A9,usb_serial=None,usb_location='1-9')],
        time_binding=asdict(TimeBinding('VALID', STAMP, 'RAWX', True, 0)),
        artifacts=[dict(artifact_id=PRIMARY, connection_id=CONNECTION, path='C:/record/raw.ubx',
            kind='PRIMARY', state='CLOSED_COMPLETE', bytes=3, sha256=HASH, utc_day=None, source_ranges=[]),
            dict(artifact_id=DAILY, connection_id=CONNECTION, path='C:/record/day.ubx',
            kind='DAILY', state='CLOSED_COMPLETE', bytes=3, sha256=HASH, utc_day='2026-10-22',
            source_ranges=[dict(journal_id=PRIMARY, start=0, end=3)])],
        stop_reason='duration', exit_code=0, integrity='VERIFIED', extensions={})


def event():
    return dict(envelope('event'), sequence=0, connection_id=None, utc=STAMP, monotonic_ns=42,
                kind='GAP_OPENED', reason='USB отключён', details=dict(gap_id='6'*32,last_data_utc=STAMP,last_rawx_utc=STAMP))


def diagnostic():
    return dict(envelope('diagnostic'), utc=STAMP, monotonic_ns=42, state='RECORDING',
        registration='ACTIVE', phase_quality='WARNINGS', hardware_validation='NOT TESTED', accounting=ledger(),
        last_data_utc=STAMP, last_rawx_utc=STAMP, last_sfrbx_utc=STAMP, last_sync_utc=STAMP,
        free_disk_bytes=None, queue_bytes=0, reconnect_attempts=0, gap_events=0, errors=[], extensions={})


def checkpoint():
    return dict(envelope('checkpoint'), journal_id=PRIMARY, utc=STAMP, synced_offset=3,
        last_chunk_sequence=0, prefix_sha256=HASH, time_binding=asdict(TimeBinding('UNDATED',None,'NONE',False,0)))


class StateAndAccountingTests(unittest.TestCase):
    def test_record_disconnect_reidentify_stop_lifecycle(self):
        lifecycle = [State.STARTING, State.IDENTIFYING, State.RECORDING, State.DISCONNECTED,
                     State.WAITING, State.IDENTIFYING, State.RECORDING, State.STOPPING, State.STOPPED]
        for before, after in zip(lifecycle,lifecycle[1:]):
            require_transition(before, after)

    def test_all_active_states_can_stop_and_storage_errors_cannot_resume(self):
        for state in State:
            if state not in (State.STOPPING, State.STOPPED):
                require_transition(state, State.STOPPING)
        for pair in ((State.WAITING,State.RECORDING), (State.STOPPED,State.STARTING),
                     (State.STORAGE_ERROR,State.RECORDING), (State.STARTING,State.RECORDING),
                     (State.RECORDING,State.RECORDING), ('BOGUS',State.STOPPED)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                require_transition(*pair)

    def test_received_bytes_survive_queue_and_short_write_accounting(self):
        ByteAccounting(**ledger(received=10,journal_written=4,journal_synced=0,pending=6,framed=0,unclassified=4))
        ByteAccounting(**ledger(received=10,journal_written=4,journal_synced=4,pending=0,unarchived=6,framed=0,unclassified=4))

    def test_inconsistent_negative_bool_and_double_counted_ledgers_rejected(self):
        for changes in (dict(received=4), dict(journal_synced=4), dict(pending=-1), dict(framed=4),
                        dict(received=True), dict(received_count_exact=1), dict(opaque=1)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ByteAccounting(**ledger(**changes))

    def test_chunks_are_immutable_and_payload_unchanged(self):
        raw=b'\xb5\x62\xff\0'
        chunk=InputChunk(CONNECTION,0,0,raw,STAMP,4)
        self.assertIs(chunk.data, raw)
        with self.assertRaises(FrozenInstanceError):
            chunk.offset=1
        for data in (bytearray(raw),None):
            with self.assertRaises(ValueError):
                InputChunk(CONNECTION,0,0,data,STAMP,4)
        with self.assertRaises(ValueError):
            InputChunk('COM10',0,0,raw,STAMP,4)

    def test_record_and_receipt_ranges(self):
        RawRecord(PRIMARY, 1, b'X', 'TAIL')
        WriteReceipt(1,2)
        for fn in (lambda: RawRecord(PRIMARY,0,b'','FRAME'), lambda: RawRecord(PRIMARY,0,b'X','REPAIRED'),
                   lambda: WriteReceipt(2,1)):
            with self.assertRaises(ValueError):
                fn()


class IdentityTimeSettingsTests(unittest.TestCase):
    def test_changed_com_is_not_identity_and_wrong_receiver_fails(self):
        good=dict(model='ZED-F9P',vid=0x1546,pid=0x01A9,usb_location='1-9',port='COM27')
        self.assertTrue(binding().matches(good))
        for changes in (dict(model='ZED-X20P'),dict(usb_location='1-10'),dict(pid=1),dict(model=None),dict(vid=True)):
            self.assertFalse(binding().matches({**good,**changes}))

    def test_serial_binding_does_not_fallback_to_location(self):
        b=DeviceBinding('ZED-X20P',0x1546,1,usb_serial='ABC')
        self.assertFalse(b.matches(dict(model='ZED-X20P',vid=0x1546,pid=1,usb_location='1-9')))
        self.assertTrue(b.matches(dict(model='ZED-X20P',vid=0x1546,pid=1,usb_serial='ABC',usb_location='9-9')))

    def test_binding_requires_single_explicit_selector(self):
        for changes in (dict(usb_location=None),dict(usb_serial='X'),dict(usb_location=''),dict(vid=65536),dict(model='UNKNOWN')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DeviceBinding(**{**asdict(binding()),**changes})

    def test_zero_or_multiple_verified_candidates_are_never_selected(self):
        candidate=dict(model='ZED-F9P',vid=0x1546,pid=0x01A9,usb_location='1-9')
        self.assertIs(require_unique_match(binding(),[candidate]),candidate)
        for candidates in ([],[candidate,dict(candidate)]):
            with self.subTest(candidates=candidates),self.assertRaises(ValueError):
                require_unique_match(binding(),candidates)

    def test_time_requires_rawx_validity_and_no_host_fallback(self):
        TimeBinding('VALID',STAMP,'RAWX',True,0)
        TimeBinding('DISCONTINUITY',None,'NONE',False,1)
        for args in [('VALID',STAMP,'HOST',True,0),('VALID',STAMP,'RAWX',False,0),
                     ('VALID','2026-10-22T23:59:59','RAWX',True,0),
                     ('UNDATED',STAMP,'RAWX',True,0),('VALID','2026-10-22T23:59:59+03:00','RAWX',True,0)]:
            with self.subTest(args=args),self.assertRaises(ValueError):
                TimeBinding(*args)

    def test_retention_is_keep_all_and_duration_explicit(self):
        RecordSettings(Path('new'), 'COM10', binding(), None)
        RecordSettings(Path('new'), 'COM10', binding(), 86400)
        for kw in (dict(seconds=0),dict(seconds=float('inf')),dict(seconds=True),dict(baud=0),
                   dict(expected_hz=0),dict(retention_policy='delete_old'),dict(station='../escape')):
            params=dict(output=Path('new'),initial_port='COM10',binding=binding(),seconds=600)
            params.update(kw)
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                RecordSettings(**params)


class SchemaTests(unittest.TestCase):
    def test_all_wire_records_round_trip_utf8(self):
        for record in (manifest(),event(),diagnostic(),checkpoint()):
            self.assertEqual(validate_record(json.loads(json.dumps(record,ensure_ascii=False))),record)

    def test_schema_snapshots_match_published_contract(self):
        for kind in ('manifest','event','diagnostic','checkpoint'):
            path=Path('docs/schemas/stage2')/(kind+'-v1.json')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),get_schema(kind))

    def test_schema_cannot_be_mutated_through_returned_copy(self):
        schema=get_schema('event')
        schema['properties']['sequence']['minimum']=-1
        self.assertEqual(get_schema('event')['properties']['sequence']['minimum'],0)

    def test_unknown_schema_version_missing_field_and_unknown_field_fail(self):
        for change in (dict(schema_version=2),dict(schema_version=True),dict(contract_version='future'),
                       dict(schema_id='gnss_logger.stage1.event'),dict(extra='ignored')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_record({**event(),**change})
        value=event(); del value['attempt_id']
        with self.assertRaises(ValueError):
            validate_record(value)

    def test_extensions_allow_finite_json_but_never_measurement_bytes_or_nan(self):
        value=manifest(); value['extensions']={'future.converter':{'status':'NOT_IMPLEMENTED'}}
        validate_record(value)
        for bad in (float('nan'),b'raw',Path('path'),{1:'not a JSON key'}):
            value=event(); value['details']={'data':bad}
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                validate_record(value)

    def test_hardware_pass_and_non_readonly_cannot_be_claimed(self):
        for update in (dict(hardware_validation='PASS'),dict(read_only=False),dict(automatic_delete=True)):
            with self.subTest(update=update),self.assertRaises(ValueError):
                validate_record({**manifest(),**update})

    def test_phase_warning_is_independent_from_active_registration(self):
        value=diagnostic()
        validate_record(value)
        self.assertEqual(value['registration'],'ACTIVE')
        self.assertEqual(value['phase_quality'],'WARNINGS')

    def test_terminal_pending_unknown_or_unsynced_bytes_cannot_claim_success(self):
        for counts in (ledger(journal_synced=2),ledger(received_count_exact=False),
                       ledger(received=4,unarchived=1),ledger(received=4,pending=1)):
            value=manifest(); value['accounting']=counts
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                validate_record(value)

    def test_partial_result_preserves_known_loss_and_uncertainty(self):
        value=manifest(); value['exit_code']=1; value['integrity']='INDETERMINATE'
        value['accounting']=ledger(received=7,unarchived=4,received_count_exact=False,journal_synced=2)
        value['artifacts'][0]['state']='CLOSED_PARTIAL'
        validate_record(value)

    def test_closed_file_needs_hash_and_open_file_cannot_be_finalized(self):
        for change in (dict(sha256=None),dict(state='OPEN'),dict(bytes=4)):
            value=manifest();value['artifacts'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_record(value)

    def test_projection_range_cannot_overlap_duplicate_or_cross_connection(self):
        for action in ('duplicate','overlap','out_of_bounds','other_connection','wrong_size','wrong_day'):
            value=manifest(); a=value['artifacts'][1]
            if action=='duplicate': value['artifacts'].append(deepcopy(a))
            if action=='overlap': a['source_ranges']=[dict(journal_id=PRIMARY,start=0,end=2),dict(journal_id=PRIMARY,start=1,end=2)]
            if action=='out_of_bounds': a['source_ranges'][0]['end']=4
            if action=='other_connection': a['connection_id']='6'*32
            if action=='wrong_size': a['bytes']=2
            if action=='wrong_day': a['utc_day']=None
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_record(value)

    def test_host_clock_jump_is_reported_and_monotonic_duration_survives(self):
        value=manifest();value['ended_utc']='2026-10-22T20:00:00Z'
        with self.assertRaises(ValueError):validate_record(value)
        value['clock_discontinuities']=1
        validate_record(value)
        self.assertEqual(value['elapsed_monotonic_ns'],2_000_000_000)

    def test_checkpoint_prefix_requires_sequence_only_for_nonempty_prefix(self):
        value=checkpoint();value['synced_offset']=0;value['last_chunk_sequence']=None
        value['prefix_sha256']=hashlib.sha256(b'').hexdigest()
        validate_record(value)
        value['last_chunk_sequence']=0
        with self.assertRaises(ValueError): validate_record(value)

    def test_manifest_cannot_assert_identity_without_exact_evidence(self):
        for change in (dict(observed_model='ZED-X20P'), dict(usb_location='1-8'),dict(match_strength='USB_SERIAL'),
                       dict(identity_status='UNIDENTIFIED'),dict(closed_utc=None)):
            value=manifest();value['connections'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError): validate_record(value)

    def test_manifest_duration_and_output_location_are_explicit(self):
        for change in (dict(seconds=None,until_stopped=False),dict(seconds=0),dict(expected_hz=float('nan')),dict(baud=True)):
            value=manifest();value['parameters'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError): validate_record(value)
        value=manifest();value['output_directory']='relative/path'
        with self.assertRaises(ValueError):validate_record(value)

    def test_events_require_the_payload_of_their_kind_and_legal_transition(self):
        value=event();value['kind']='STATE_CHANGED'
        with self.assertRaises(ValueError):validate_record(value)
        value['details']=dict(before='WAITING',after='RECORDING')
        with self.assertRaises(ValueError):validate_record(value)
        value['details']=dict(before='WAITING',after='IDENTIFYING')
        validate_record(value)

    def test_projection_cannot_reorder_the_original_ranges(self):
        value=manifest()
        value['artifacts'][1]['source_ranges']=[dict(journal_id=PRIMARY,start=2,end=3),dict(journal_id=PRIMARY,start=0,end=2)]
        with self.assertRaises(ValueError):validate_record(value)


class CLICompatibilityTests(unittest.TestCase):
    def test_stage1_defaults_and_aliases_unchanged(self):
        p=parser()
        for command in ('serial','diagnose'):
            args=p.parse_args([command,'--port','COM10'])
            self.assertEqual((args.seconds,args.baud,args.expected_hz),(60,115200,1))
            self.assertFalse(args.inspect)
        self.assertEqual(p.parse_args(['replay','old.ubx']).speed,0)
        self.assertEqual(p.parse_args(['mock']).profile,'f9p')
        self.assertEqual(p.parse_args(['ports']).format,'text')

    def test_all_reserved_commands_fail_explicitly_without_any_io(self):
        commands=[['record','--output','never-created','--port','COM10','--model','ZED-F9P',
            '--vid','0x1546','--pid','0x01a9','--usb-location','1-9','--until-stopped'],
            ['status','not-read'],['verify','not-read'],['recover','not-read','--check'],
            ['recover','not-read','--output','never-created']]
        for argv in commands:
            with self.subTest(argv=argv), patch('gnss_logger.cli.open_port') as port, \
                    patch('gnss_logger.cli.Session') as session, patch('builtins.open') as opened, \
                    patch.object(Path,'open') as path_open, patch.object(Path,'mkdir') as mkdir:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code=main(argv)
                result=json.loads(output.getvalue())
                self.assertEqual((code,result['status']),(4,'NOT_IMPLEMENTED'))
                for operation in (port,session,opened,path_open,mkdir):operation.assert_not_called()

    def test_new_options_reject_unsafe_or_ambiguous_requests(self):
        base=['record','--output','new','--port','COM10','--model','ZED-F9P','--vid','0x1546','--pid','0x01a9']
        variations=[['--until-stopped'],['--usb-location','1-9'],
            ['--usb-location','1-9','--usb-serial','X','--until-stopped'],
            ['--usb-location','1-9','--seconds','10','--until-stopped'],
            ['--usb-location','1-9','--seconds','nan'],
            ['--usb-location','1-9','--until-stopped','--baud','0'],
            ['--usb-location','','--until-stopped'],
            ['--usb-location','1-9','--until-stopped','--retention-policy','delete_old']]
        for args in variations:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(base+args)
            self.assertEqual(raised.exception.code,2)

    def test_recover_requires_explicit_check_or_new_output(self):
        for args in (['recover','old'],['recover','old','--check','--output','new']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):main(args)

    def test_stage2_help_is_available_without_starting_session(self):
        for name in ('record','status','verify','recover'):
            with patch('gnss_logger.cli.Session') as session, contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
                main([name,'--help'])
            self.assertEqual(raised.exception.code,0)
            self.assertIn('Stage 2',output.getvalue())
            session.assert_not_called()
