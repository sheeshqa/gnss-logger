"""Optional GUI entry point; CLI and imports stay usable without Qt installed."""
import argparse
import json
import sys
import tempfile
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description='GNSS Logger: recording and UBX to RINEX conversion GUI')
    parser.add_argument('--mock', action='store_true', help='synthetic receiver and real recorder with injected source; no COM')
    parser.add_argument('--mock-root', help='persistent mock directory beneath the temporary GNSS-GUI-MOCK directory')
    parser.add_argument('--smoke-test', action='store_true', help='automated mock lifecycle and exit')
    parser.add_argument('--acceptance-smoke', type=Path, help='write isolated mock acceptance JSON report')
    parser.add_argument('--smoke-images', type=Path, help='directory for mock acceptance screenshots')
    args = parser.parse_args(argv)
    if args.acceptance_smoke and (not args.mock or args.smoke_test):
        parser.error('--acceptance-smoke requires --mock and cannot be combined with --smoke-test')
    if args.smoke_images and not args.acceptance_smoke:
        parser.error('--smoke-images requires --acceptance-smoke')
    if args.smoke_test and not args.mock:
        parser.error('--smoke-test requires --mock')
    mock_root = None
    if args.mock_root:
        if not args.mock:
            parser.error('--mock-root requires --mock')
        candidate = Path(args.mock_root).expanduser()
        if not candidate.is_absolute():
            parser.error('--mock-root must be absolute')
        mock_root = candidate.resolve()
        sandbox = (Path(tempfile.gettempdir()) / 'GNSS-GUI-MOCK').resolve()
        if mock_root == sandbox or not mock_root.is_relative_to(sandbox):
            parser.error('--mock-root must be a child of ' + str(sandbox))
    if not (3, 10) <= sys.version_info[:2] < (3, 16):
        message = 'GUI requires Python 3.10–3.15; the independent CLI remains available.'
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
    except ModuleNotFoundError as error:
        if not (error.name or '').startswith('PySide6'):
            raise
        message = 'Optional GUI dependency missing. Install gnss-logger[gui] in a separate GUI environment.'
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        elif sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'GNSS Logger', 0x10)
        return 2
    from .main_window import MainWindow
    from .receivers import ReceiverBackend, MockReceiverBackend

    app = QApplication([sys.argv[0]])
    app.setApplicationName('GNSS Logger')
    app.setOrganizationName('GNSS Logger')
    # Inno Setup checks this presence marker before replacing application files.
    # It is not a single-instance restriction and does not replace DeviceRegistry.
    mutex = None
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        mutex = kernel.CreateMutexW(None, False, 'Local\\GNSSLogger.GUI')
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
    window = MainWindow(MockReceiverBackend() if args.mock else ReceiverBackend(), operational=True, mock_root=mock_root)
    window.show()
    if args.acceptance_smoke:
        from .acceptance import run_acceptance
        run_acceptance(app, window, args.acceptance_smoke.resolve(),
                       (args.smoke_images or args.acceptance_smoke.parent / 'images').resolve())
    smoke = {'heartbeat': 0, 'identified': False, 'started': False, 'stopped': False, 'timeout': False}
    if args.smoke_test:
        mock_root = window.mock_root
        smoke['output'] = str(mock_root)
        timer = QTimer(window)
        timer.setInterval(25)

        def drive():
            smoke['heartbeat'] += 1
            coordinator = window.coordinator
            if coordinator.devices and not coordinator.busy and not smoke['identified']:
                smoke['identified'] = coordinator.identify(seconds=2)
            if coordinator.view()['can_start'] and not smoke['started']:
                smoke['started'] = coordinator.start_recording(dict(
                    output=mock_root/'runs', daily_root=mock_root/'daily', seconds=None,
                    station='MOCK', expected_hz=1.0))
            for event in window.last_events:
                raw = event['data'].get('recording') or {}
                epochs = raw.get('extensions', {}).get('gnss_logger.diagnostics', {}).get('RAWX', {}).get('epochs', 0)
                if epochs >= 2 and not smoke['stopped']:
                    smoke['stopped'] = coordinator.stop_recording()
            if window.result_received and not coordinator.busy:
                coordinator.request_shutdown()

        def timeout():
            if not window.coordinator.shutdown_complete:
                smoke['timeout'] = True
                window.coordinator.request_shutdown()

        timer.timeout.connect(drive)
        timer.start()
        QTimer.singleShot(0, window.coordinator.refresh_devices)
        QTimer.singleShot(15000, timeout)
    try:
        code = app.exec()
    finally:
        if mutex:
            kernel.CloseHandle(mutex)
    if args.smoke_test:
        report = getattr(window, 'latest_record_report', None) or {}
        detail = report.get('extensions', {}).get('gnss_logger.recording', {})
        passed = (smoke['started'] and smoke['identified'] and smoke['stopped']
                  and not smoke['timeout'] and smoke['heartbeat'] > 1
                  and window.result_received and window.delivery_on_ui_thread
                  and window.coordinator.shutdown_complete and detail.get('exit_code') == 0
                  and report.get('registration') == 'STOPPED_COMPLETE'
                  and report.get('accounting', {}).get('unarchived') == 0)
        if sys.stdout is not None:
            print(json.dumps(dict(smoke=smoke, passed=passed,
                                  ui_thread_delivery=window.delivery_on_ui_thread)))
        return 0 if passed else 1
    return code
