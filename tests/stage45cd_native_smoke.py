"""Manual native acceptance matrix, deliberately outside test discovery.

Run with the existing GUI Python, after GUI-ready and the test slot is released:
    python tests/stage45cd_native_smoke.py --output <fresh-directory>
    python tests/stage45cd_native_smoke.py --exe <windowed-exe> --output <fresh-directory>
Omit --exe to check source only; pass --frozen-only to check only the bundle.
Each scale and restart phase uses a separate OS process. Reports are read from
disk, so windowed executables need no console. No dependency install or build.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

REPOSITORY = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--frozen-only', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('Native Windows required')
    if args.frozen_only and not args.exe:
        parser.error('--frozen-only requires --exe')
    if args.exe and not args.exe.is_file():
        parser.error('--exe must exist')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    foreign_cwd = output / ('произвольный рабочий каталог ' + uuid4().hex[:12])
    foreign_cwd.mkdir(exist_ok=False)
    probe_env = os.environ.copy()
    for key in list(probe_env):
        if key.upper() in ('QT_SCALE_FACTOR', 'QT_SCREEN_SCALE_FACTORS', 'QT_FONT_DPI'):
            del probe_env[key]
    probe_env['QT_QPA_PLATFORM'] = 'windows'
    probe = subprocess.run([sys.executable, '-c',
        'from PySide6.QtWidgets import QApplication; a=QApplication([]); print(a.primaryScreen().devicePixelRatio())'],
        env=probe_env, capture_output=True, check=True, timeout=30)
    host_dpr = float(probe.stdout.decode('ascii').strip())
    modes = [] if args.frozen_only else [('source', [sys.executable, '-m', 'gnss_logger.gui'])]
    if args.exe:
        modes.append(('frozen', [str(args.exe.resolve())]))
    results = []
    started = time.monotonic()
    for mode, command in modes:
        for scale in ('1', '1.25', '1.5'):
            label = mode + '-' + scale.replace('.', '_')
            # app CLI deliberately confines mock roots below this directory.
            root = Path(tempfile.gettempdir()) / 'GNSS-GUI-MOCK' / ('acceptance-' + uuid4().hex[:12])
            root.mkdir(parents=True)
            for phase in ('write', 'read'):
                report = output / (label + '-' + phase + '.json')
                images = output / (label + '-' + phase)
                env = os.environ.copy()
                for key in list(env):
                    if key.upper() in ('QT_SCREEN_SCALE_FACTORS', 'QT_FONT_DPI'):
                        del env[key]
                env.update(QT_QPA_PLATFORM='windows', QT_SCALE_FACTOR=str(float(scale)/host_dpr),
                           GNSS_ACCEPTANCE_DPR=scale, GNSS_ACCEPTANCE_PHASE=phase, PYTHONUTF8='1')
                cwd = REPOSITORY
                if mode == 'frozen':
                    cwd = foreign_cwd
                    for key in list(env):
                        if key.upper() in ('PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
                            del env[key]
                    windows = Path(env.get('SystemRoot', 'C:/Windows'))
                    env['PATH'] = os.pathsep.join(str(p) for p in
                        (windows / 'System32', windows))
                launch = command + ['--mock', '--mock-root', str(root),
                                    '--acceptance-smoke', str(report), '--smoke-images', str(images)]
                item = dict(mode=mode, scale=scale, phase=phase, report=str(report), command=launch,
                            host_dpr=host_dpr, qt_scale_factor=env['QT_SCALE_FACTOR'],
                            cwd=str(cwd), path=env.get('PATH'),
                            pythonhome_present=any(k.upper() == 'PYTHONHOME' for k in env),
                            pythonpath_present=any(k.upper() == 'PYTHONPATH' for k in env))
                try:
                    process = subprocess.run(launch, cwd=cwd, env=env,
                                             capture_output=True, timeout=args.timeout)
                    item['process_exit_code'] = process.returncode
                    item['stdout'] = process.stdout.decode('utf-8', errors='replace')[-12000:]
                    item['stderr'] = process.stderr.decode('utf-8', errors='replace')[-12000:]
                    if report.is_file():
                        detail = json.loads(report.read_text(encoding='utf-8'))
                        item['acceptance'] = detail
                        screenshots = detail.get('screenshots', [])
                        item['passed'] = bool(process.returncode == 0 and detail.get('passed')
                            and detail.get('serial_open_calls') == 0 and detail.get('subprocess_calls') == 0
                            and detail.get('shutdown_complete_all_four')
                            and len(screenshots) == 4 and all(Path(p).is_file() for p in screenshots)
                            and (phase != 'read' or detail.get('restart_verified')))
                    else:
                        item.update(passed=False, error='No JSON report written by GUI')
                except Exception as error:
                    item.update(passed=False, error=type(error).__name__ + ': ' + str(error))
                results.append(item)
                aggregate = dict(passed=all(r['passed'] for r in results), results=results,
                                 elapsed_s=round(time.monotonic()-started, 3))
                print(f"{label} {phase}: {'PASS' if item['passed'] else 'FAIL'}", flush=True)
                if not item['passed']:
                    break
    passed = len(results) == len(modes)*6 and all(r['passed'] for r in results)
    aggregate.update(passed=passed, elapsed_s=round(time.monotonic()-started, 3))
    with (output / 'aggregate.json').open('x', encoding='utf-8') as stream:
        json.dump(aggregate, stream, ensure_ascii=False, indent=2)
    print(str(output / 'aggregate.json'), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
