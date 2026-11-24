"""Hardware-free contracts for bounded pull monitoring and result presentation."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from threading import get_ident
from unittest import TestCase

from gnss_logger.application.monitoring import OperationMonitor
from gnss_logger.application.results import present_result


class Handle:
    def __init__(self):
        self.value = {'state': 'PENDING', 'progress': None}
        self.report = None
        self.callers = []

    def snapshot(self):
        self.callers.append(get_ident())
        return self.value

    def result(self):
        self.callers.append(get_ident())
        return self.report


class ResultTests(TestCase):
    def test_extension_exit_code_and_core_diagnostic_warnings(self):
        report = {'extensions': {
            'gnss_logger.recording': {'exit_code': 1},
            'gnss_logger.diagnostics': {'decode_errors': {'bad': 2}, 'checksum_errors': 3,
                'timing': {'gap_events': 4}, 'phase_quality': {'status': 'WARNINGS', 'warnings': ['phase warning']}}}}
        original = deepcopy(report)
        view = present_result(report)
        self.assertEqual((view['lifecycle'], view['exit_code']), ('FAILED', 1))
        self.assertEqual({w['diagnostic'] for w in view['warnings'] if isinstance(w, dict)},
                         {'decode_errors', 'checksum_errors', 'gap_events', 'phase_quality'})
        self.assertIn('phase warning', view['warnings'])
        self.assertEqual(report, original)
        report['extensions']['gnss_logger.recording']['exit_code'] = 130
        self.assertEqual(present_result(report)['lifecycle'], 'CANCELLED')

    def test_actual_phase_statuses(self):
        for status in ('NO_INDICATORS_DETECTED', 'NO_DATA'):
            view = present_result({'diagnostics': {'phase_quality': {'status': status}}, 'exit_code': 0})
            self.assertEqual(view['warnings'], [])
            self.assertEqual(view['phase_quality'], status)
        view = present_result({'phase_quality': 'WARNINGS', 'exit_code': 0})
        self.assertEqual(view['warnings'], [{'diagnostic': 'phase_quality', 'value': 'WARNINGS'}])

    def recording(self, conversions=(), **changes):
        report = dict(registration={'status': 'STOPPED_COMPLETE', 'complete': True},
                      exit_code=0, integrity='VERIFIED', stop_reason='deadline', warnings=[])
        report.update(changes)
        original = deepcopy(report)
        result = present_result(report, conversions=conversions)
        self.assertEqual(report, original)
        self.assertEqual(result['registration'], report['registration'])
        return result

    def test_registration_success_conversion_not_run(self):
        view = self.recording()
        self.assertEqual(view['lifecycle'], 'FINISHED')
        self.assertIsNone(view['conversion'])

    def test_registration_success_conversion_success(self):
        self.assertEqual(self.recording([{'status': 'SUCCESS', 'exit_code': 0}])['conversion']['status'], 'SUCCESS')

    def test_registration_success_conversion_cancelled(self):
        self.assertEqual(self.recording([{'status': 'INTERRUPTED', 'exit_code': 130}])['lifecycle'], 'FINISHED')

    def test_registration_success_conversion_failed(self):
        self.assertEqual(self.recording([{'status': 'FAILED', 'exit_code': 1}])['lifecycle'], 'FINISHED')

    def test_obs_only_exit_three_preserved(self):
        report = {'status': 'OBS_ONLY_INCOMPLETE_NAVIGATION', 'exit_code': 3}
        view = present_result(report, 'conversion')
        self.assertEqual(view['conversion'], report)
        self.assertEqual(view['exit_code'], 3)
        self.assertEqual(view['lifecycle'], 'FAILED')

    def test_skipped_exit_zero(self):
        view = present_result({'status': 'SKIPPED', 'exit_code': 0}, 'conversion')
        self.assertEqual(view['lifecycle'], 'FINISHED')
        self.assertEqual(view['conversion']['status'], 'SKIPPED')

    def test_warnings_detached(self):
        report = {'exit_code': 0, 'warnings': [{'message': 'notice'}]}
        view = present_result(report)
        view['warnings'][0]['message'] = 'changed'
        self.assertEqual(report['warnings'][0]['message'], 'notice')

    def test_usb_partial_integrity_and_reason_independent(self):
        view = self.recording(registration={'status': 'PARTIAL', 'complete': False},
                              integrity='INDETERMINATE', stop_reason='usb_disconnect', exit_code=1)
        self.assertEqual((view['lifecycle'], view['integrity'], view['stop_reason']),
                         ('FAILED', 'INDETERMINATE', 'usb_disconnect'))

    def test_cancel_preserves_previous_published_segments(self):
        report = dict(status='INTERRUPTED', exit_code=130, segments=[
            {'status': 'SUCCESS', 'published_outputs': {'obs': 'previous.obs', 'nav': 'previous.nav'}},
            {'status': 'INTERRUPTED', 'published_outputs': []}])
        view = present_result(report, 'conversion')
        self.assertEqual(view['published_files'], ['previous.obs', 'previous.nav'])
        self.assertEqual(view['lifecycle'], 'CANCELLED')

    def test_controller_extensions_and_diagnosis(self):
        report = dict(exit_code=0, extensions={
            'gnss_logger.recording': {'integrity': 'VERIFIED', 'stop_reason': 'deadline'},
            'gnss_logger.conversion': {'statuses': {'SUCCESS': 1}},
            'gnss_logger.diagnostics': {'phase_quality': {'status': 'GOOD'}}})
        self.assertEqual(present_result(report)['conversion']['statuses'], {'SUCCESS': 1})
        diagnosis = dict(diagnostics={'phase_quality': {'status': 'GOOD'}}, identity={'model': 'x'},
                         errors=[], warnings=['w'], run={'id': 'r'}, exit_code=0, stop_reason='deadline')
        view = present_result(diagnosis, 'diagnosis')
        for key in ('diagnostics', 'identity', 'errors', 'warnings', 'run', 'exit_code', 'stop_reason'):
            self.assertEqual(view[key], diagnosis[key])


class MonitorTests(TestCase):
    def test_auto_conversion_error_is_visible_without_failing_recording(self):
        handle = Handle()
        handle.value = {'lifecycle':'RUNNING','recording':{'registration':'ACTIVE'},
                        'auto_conversion':{'last_error':'convbin failed'}}
        monitor = OperationMonitor(handle,'recording',capacity=1)
        monitor.refresh()
        self.assertEqual(monitor.critical['data']['error'],'convbin failed')
        handle.report = {'registration':'STOPPED_COMPLETE', 'exit_code':0,
                         'extensions':{'gnss_logger.conversion':{'statuses':{'FAILED':1},'last_error':'convbin failed'}}}
        monitor.refresh()
        self.assertEqual(monitor.terminal['data']['registration'],'STOPPED_COMPLETE')
        self.assertEqual(monitor.terminal['data']['conversion']['statuses'],{'FAILED':1})

    def test_result_exception_retains_latest_recording(self):
        handle = Handle()
        monitor = OperationMonitor(handle, 'recording')
        monitor.refresh()
        recording = {'registration': {'status': 'STOPPED_COMPLETE', 'complete': True},
                     'integrity': 'VERIFIED', 'exit_code': 0, 'artifacts': [{'path': 'capture.ubx'}]}
        handle.value = {'lifecycle': 'FINALIZING', 'recording': recording}
        original = deepcopy(handle.value)
        def broken():
            raise RuntimeError('final result unavailable')
        handle.result = broken
        terminal = monitor.refresh()
        self.assertEqual(terminal['lifecycle'], 'FAILED')
        self.assertEqual(terminal['data']['registration'], recording['registration'])
        self.assertEqual(terminal['data']['artifacts'], recording['artifacts'])
        self.assertEqual(terminal['data']['exit_code'], 0)
        self.assertEqual(terminal['data']['api_error']['error_type'], 'RuntimeError')
        self.assertEqual(handle.value, original)

    def test_result_or_snapshot_exception_retains_published_conversion(self):
        for failing_method in ('result', 'snapshot'):
            with self.subTest(failing_method=failing_method):
                handle = Handle()
                progress = {'status': 'RUNNING', 'segments': [{'status': 'SUCCESS',
                    'published_outputs': {'obs': 'previous.obs', 'nav': 'previous.nav'}}],
                    'errors': ['earlier diagnostic']}
                handle.value = {'state': 'RUNNING', 'progress': progress}
                monitor = OperationMonitor(handle, 'conversion', capacity=1)
                monitor.refresh()
                def broken():
                    raise ValueError('source failed')
                setattr(handle, failing_method, broken)
                terminal = monitor.refresh()
                self.assertEqual(terminal['lifecycle'], 'FAILED')
                self.assertEqual(terminal['data']['published_files'], ['previous.obs', 'previous.nav'])
                self.assertEqual(terminal['data']['conversion'], progress)
                self.assertEqual(terminal['data']['api_error']['message'], 'source failed')
                self.assertEqual(monitor.critical['data']['errors'], ['earlier diagnostic'])
                self.assertEqual(monitor.critical['data']['errors_total'], 1)
                terminal['data']['segments'].clear()
                self.assertEqual(len(monitor.terminal['data']['segments']), 1)

    def test_overflow_sticky_cumulative_and_terminal(self):
        handle = Handle()
        monitor = OperationMonitor(handle, 'conversion', capacity=1)
        for index in range(1000):
            handle.value = {'state': 'RUNNING', 'progress': {'errors': ['first'], 'n': index}}
            monitor.refresh()
            self.assertLessEqual(monitor.queued_count, 3)
        handle.report = {'exit_code': 1, 'errors': ['first'], 'segments': []}
        monitor.refresh()
        events = monitor.drain()
        self.assertIn('critical', [event['kind'] for event in events])
        self.assertIn('result', [event['kind'] for event in events])
        self.assertEqual(monitor.queued_count, 0)
        self.assertEqual(monitor.critical['data']['errors'], ['first'])
        terminal = monitor.terminal
        terminal['data']['errors'].clear()
        self.assertEqual(monitor.terminal['data']['errors'], ['first'])
        self.assertEqual(monitor.refresh(), monitor.terminal)

    def test_recording_wrapper_lifecycles_and_bounded_errors(self):
        handle = Handle()
        monitor = OperationMonitor(handle, 'recording')
        fixed_id = monitor.operation_id
        for state in ('PENDING', 'RUNNING', 'STOPPING', 'FINALIZING'):
            handle.value = dict(operation_id='run-id', lifecycle=state, recording={
                'extensions': {'gnss_logger.recording': {'errors': ['bounded'], 'errors_total': 900}}})
            self.assertEqual(monitor.refresh()['lifecycle'], state)
        self.assertEqual(monitor.critical['data']['errors_total'], 900)
        self.assertEqual(monitor.critical['operation_id'], fixed_id)

    def test_source_exception_preserves_cumulative_errors(self):
        handle = Handle()
        handle.value = {'state': 'RUNNING', 'progress': {'extensions': {
            'gnss_logger.recording': {'errors': ['original'], 'errors_total': 900}}}}
        monitor = OperationMonitor(handle, 'recording')
        monitor.refresh()
        def broken():
            raise ValueError('snapshot failed')
        handle.snapshot = broken
        monitor.refresh()
        self.assertEqual(monitor.critical['data'], dict(errors=['original'], errors_total=900, error='snapshot failed'))

    def test_future_pending_running_done_never_blocks(self):
        future = Future()
        monitor = OperationMonitor(future, 'diagnosis')
        self.assertEqual(monitor.refresh()['lifecycle'], 'PENDING')
        future.set_running_or_notify_cancel()
        self.assertEqual(monitor.refresh()['lifecycle'], 'RUNNING')
        future.set_result({'exit_code': 0, 'diagnostics': {'bytes': 8}, 'identity': {'model': 'x'}})
        self.assertEqual(monitor.refresh()['data']['diagnostics'], {'bytes': 8})

    def test_future_cancel_and_exception(self):
        for cancelled in (True, False):
            future = Future()
            future.cancel() if cancelled else future.set_exception(ValueError('broken'))
            monitor = OperationMonitor(future, 'diagnosis')
            self.assertEqual(monitor.refresh()['lifecycle'], 'CANCELLED' if cancelled else 'FAILED')
            self.assertTrue(monitor.critical['data']['error'])

    def test_sync_report_and_event_detachment(self):
        report = {'exit_code': 0, 'warnings': ['original']}
        monitor = OperationMonitor(report, 'diagnosis', operation_id='chosen')
        monitor.refresh()['data']['warnings'].clear()
        report['warnings'].clear()
        self.assertEqual(monitor.terminal['data']['warnings'], ['original'])
        for event in monitor.drain():
            self.assertEqual(set(event), {'operation_id', 'operation_type', 'lifecycle', 'utc', 'sequence', 'kind', 'data'})
            self.assertEqual(event['operation_id'], 'chosen')

    def test_no_producer_callbacks_or_threads(self):
        handle = Handle()
        monitor = OperationMonitor(handle, 'conversion')
        with ThreadPoolExecutor(max_workers=1) as executor:
            producer = executor.submit(lambda: get_ident()).result()
        self.assertEqual(handle.callers, [])
        monitor.refresh()
        self.assertEqual(handle.callers, [get_ident(), get_ident()])
        self.assertNotEqual(producer, get_ident())

    def test_capacity_validation_and_empty_poll(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                OperationMonitor(Handle(), 'recording', capacity=value)
        self.assertIsNone(OperationMonitor(Handle(), 'recording').poll())
