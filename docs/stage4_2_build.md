# Stage 4.2 skeleton build

Implements the [Stage 4.1 packaging strategy](stage4_1_packaging_strategy.md),
using its [verified compatibility baseline](stage4_1_framework_compatibility.md)
and [GUI entry-point design](stage4_1_gui_design.md). This is a local skeleton
build, not an installer or release acceptance. Coordinated native builds and
mock smoke checks were subsequently completed; see the measured results below.

## Environment and commands

Use the existing isolated Windows x64 CPython 3.15.1 environment. No install in
the recording environment is needed. From the repository root in PowerShell:

```powershell
$buildPython = 'C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-GFsLUl/gnss-stage41-07a831f423964f6aa7500b51ef39bdc3/venv/Scripts/python.exe'
& $buildPython tools/build_gui_stage4_2.py --dry-run
# Run only after coordination with the GUI implementation/test owner:
& $buildPython tools/build_gui_stage4_2.py --output-root "$env:TEMP/gnss-stage4-2" --build-id skeleton-001
```

The default output root is the system temp directory plus `gnss-stage4-2`;
the default id combines a UTC timestamp and UUID. An explicit id must be a
single ASCII letter/digit-led name (letters, digits, underscore, hyphen; max 80).
Existing run directories are refused, even after a failed build. Use a new id
for every attempt. `--dry-run` prints JSON argument vectors without creating
directories or invoking PyInstaller. Add `--gui-console` for diagnostic builds.

The wrapper uses its own interpreter, argument lists and `shell=False`. It
does not delete files, pass `--noconfirm`, install dependencies, invoke hardware,
or launch either application. Every run reserves a fresh directory exclusively;
spec files, work trees, logs and distribution files stay below that directory.
The repository root is supplied through `--paths`; launch does not depend on cwd.
Do not concurrently edit GUI sources during a coordinated build: the build does
not snapshot or lock the checkout.

## Payload and manual smoke checks

```text
<output-root>/<build-id>/
  commands.json
  launch_cli_stage4_2.py       # generated inside the run, not a repo change
  gnss-logger-gui.build.log
  gnss-logger.build.log
  spec/<executable-name>/
  work/<executable-name>/      # includes PyInstaller warnings
  dist/
    gnss-logger-gui/gnss-logger-gui.exe  # windowed by default, own _internal
    gnss-logger/gnss-logger.exe          # console, own _internal
```

Two independent onedir bundles keep CLI imports independent of Qt; they are a
minimal first artifact, not a size-optimized shared COLLECT. Keep each entire
onedir folder together. The GUI launcher imports `gnss_logger.gui.app:main` and
passes process arguments untouched, including `--mock` and `--smoke-test`.
The generated CLI launcher calls `gnss_logger.cli:main` with the existing contract.

After the owner approves/runs the build, from a cwd outside the bundle:

```powershell
$bundle = "$env:TEMP/gnss-stage4-2/skeleton-001/dist"
$smoke = Start-Process -FilePath "$bundle/gnss-logger-gui/gnss-logger-gui.exe" -ArgumentList '--mock','--smoke-test' -PassThru -Wait
$smoke.ExitCode
& "$bundle/gnss-logger/gnss-logger.exe" --help
```

The source equivalent is `& $buildPython tools/launch_gui_stage4_2.py --mock --smoke-test`.
The app implementation owns mock safety, smoke assertions and shutdown. No COM
commands are part of this procedure. A windowed EXE has no reliable console
output; use its waited process exit code and optionally the console build for
diagnostics. Inspect build logs/warnings and Qt DLL/platform plugin inventory.
Native launch, DPI/Unicode paths and clean Windows 10/11 without Python remain
separate checks; a successful build alone does not prove them.

## Dependency baseline

Already available in the specified temp environment; do not reinstall for this
task. For a future separate environment, the initial requirements text is:

```text
PySide6-Essentials==6.12.0
shiboken6==6.12.0
PyInstaller==6.22.3
pyinstaller-hooks-contrib==2026.10
pyserial==3.5
```

These are the primary baseline pins, not a complete hashed dependency lock.
Freeze remaining transitive build dependencies and wheel hashes before release
reproduction. The process is repeatable into fresh directories; byte-identical
binaries are not claimed. No convbin, personal TOML, scientific data, explicit
resource trees or installer are bundled by this wrapper. Add required future
resources deliberately, and complete the licensing/release gates from Stage 4.1
before distribution.

## Measured final build (2026-11-24)

After the GUI owner froze the queued idle-close fix, the earlier diagnostic
build finished before one new final build started. No builds overlapped.
The final command used the existing temp interpreter and
`tools/build_gui_stage4_2.py --build-id final-frozen-20261124-a1`.
Its run directory is
`C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-GFsLUl/gnss-stage4-2/final-frozen-20261124-a1`.
Both PyInstaller invocations and the wrapper exited 0.

| Bundle | Files | Bytes | Approximate MiB | PE subsystem |
| --- | ---: | ---: | ---: | --- |
| GUI | 165 | 102,892,327 | 98.13 | 2 (Windows GUI) |
| CLI | 24 | 24,420,316 | 23.29 | 3 (Windows console) |

`source-hashes-before.json` and `source-hashes-after.json` in that run directory
match for all 69 entries: package Python sources, project metadata and both
repository launch/build scripts. The wrapper now records those snapshots and
fails if they differ after successful builds. No GUI sources were edited by
this task. The source snapshots do not include documentation or tests.

Native launches used cwd outside the bundles, removed PYTHONHOME/PYTHONPATH,
and restricted PATH to Windows and System32. GUI `--mock --smoke-test` exited 0:
`heartbeat=80`, `started=true`, `timeout=false`, `passed=true`,
`ui_thread_delivery=true`. CLI `--help` exited 0 and listed the existing commands.
Captured results and inventory are preserved in `smoke-results.json` in the run
directory. No full test suite was started by this task; the main owner runs it
sequentially after the builds. Python is installed on this host, so these checks
do not establish clean-machine acceptance.

GUI Qt libraries: Qt6Core, Qt6Gui, Qt6Network, Qt6Svg and Qt6Widgets.
The 20 plugin DLLs comprise generic/qtuiotouchplugin; iconengines/qsvgicon;
imageformats/qgif, qicns, qico, qjpeg, qsvg, qtga, qtiff, qwbmp, qwebp;
networkinformation/qnetworklistmanager; platforms/qdirect2d, qminimal, qoffscreen,
qwindows; styles/qmodernwindowsstyle; tls/qcertonlybackend, qopensslbackend,
qschannelbackend. The CLI contains no Qt DLLs or platform plugins.

Each build log has two WARNING entries about ignored macOS CoreFoundation and
IOKit ctypes paths from pySerial's list_ports_osx backend. They concern a
non-Windows backend; the tested Windows startup paths passed. Each analysis
warning file has 30 missing/excluded entries, including Unix/macOS modules,
the optional .NET serial backend (System/System.IO), typing/build-time helpers,
collections.abc, frozen import machinery and multiprocessing attributes.
Neither warning file reports a missing gnss_logger or PySide6 module.
These warnings are preserved for review, not blanket-dismissed: successful
mock GUI startup and CLI help do not exercise every frozen core operation.
Warning files are under `work/<name>/<name>/warn-<name>.txt`; complete analysis
cross-references and logs remain alongside the final build.
