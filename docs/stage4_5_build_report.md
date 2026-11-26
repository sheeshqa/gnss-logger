# Stage 4.5d — Windows build and local evaluation

Date: 2026-11-26. **Build and technical acceptance PASS**, with the release gates below.
Evidence chronology reconciled: the failed QT_SCREEN_SCALE_FACTORS=1 attempt
preceded the BF663 acceptance correction and the final source/frozen matrices.
All six final frozen rows independently confirm actual DPR 1/1.25/1.5 using
Qt multipliers 0.8/1/1.2 at host DPR 1.25. Final source snapshot and build manifest
contain the corrected acceptance SHA256; source-verification matched=true.
The temporary UNACCEPTED hold is resolved by this evidence; no further rebuild
or guide edit was necessary. Private local evaluation only; public release remains blocked.

## Final deliverables — private local evaluation

All generated artifacts are outside Git. These corrected artifacts supersede the
initial installer; the old artifacts and failure evidence remain unchanged.

| Deliverable | Actual size | SHA256 |
| --- | ---: | --- |
| [Portable ZIP](../../../reports/stage45d-final-silent-20261126/GNSSLogger-portable.zip) | 48,299,219 bytes | `0115b3cec7fcba7c57a0594ffdaa96e761693752013135734a6fceb75309fa55` |
| [Inno installer](../../../reports/stage45d-final-silent-20261126/GNSSLogger-0.5.0.45-setup.exe) | 31,805,258 bytes | `c9839e779cf84b87ebefcd252413805347db87417ce0c20664fe33ea993d2bfe` |

- [Shared GUI/CLI onedir](../../../reports/stage45d-final-silent-20261126/dist/GNSSLogger/).
- [Checksums](../../../reports/stage45d-final-silent-20261126/SHA256SUMS.txt) and
  [build manifest](../../../reports/stage45d-final-silent-20261126/dist/GNSSLogger/build-manifest.json).
- [Source capture](../../../reports/stage45d-final-silent-20261126/source-snapshot.json),
  [final verification](../../../reports/stage45d-final-silent-20261126/source-verification.json),
  [verified freeze reuse](../../../reports/stage45d-final-silent-20261126/freeze-reuse.json).
- [Actual corrected lifecycle results](../../../reports/stage45d-silent-lifecycle-20261126/validation.json).

## Scope and toolchain

Only [packaging](../packaging/), [new build tooling](../tools/build_stage45d.py)
and this report were edited by this task. Existing GUI/app/pages/core were not
edited. The complete Stage 4.1 packaging strategy, Stage 4.5a–b report and Python
fact-grounded coding skill were read as UTF-8. Parent owns GUI/mutex/acceptance
integration. No hardware, actual convbin, commit or push was used. The main project
environment was untouched; dependencies were installed only in the existing
isolated GUI environment.

Observed host: Windows 10 x64 10.0.19045, CPython 3.15.1,
PySide6-Essentials/shiboken6 6.12.0, pySerial 3.5, PyInstaller 6.22.3,
hooks-contrib 2026.10. [Ten-wheel lock](../packaging/build-requirements.lock) pins
versions and SHA256. Offline hash-checked dependency resolution passed without
network or installation; downloaded wheels remain outside Git.

No existing Inno compiler was found in standard system/per-user locations or PATH.
Inno Setup 6.7.4 was downloaded from the GitHub release linked by the official
JRSoftware download page and installed per-user under a dedicated reports toolchain
directory, without administrator or global installation. Authenticode was **Valid**,
with publisher `CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL`, matching the
official publisher. [Provenance](../packaging/toolchain-provenance.json):

- Official index: https://jrsoftware.org/isdl.php
- Download: https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe
- Download SHA256: `a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c`.
- ISCC SHA256: `f02fa0e5f237b47d7ed1edae9b991866ec94fe02af61eb6a73e94f92fe8e0604`.

## Build and byte provenance

[One spec](../packaging/gnss.spec) builds a windowed GUI and console CLI in one
shared onedir, without duplicate Qt trees. PE inspection confirmed GUI subsystem 2,
CLI subsystem 3 and icons in both EXEs. GUI retains unittest.mock for the opt-in
acceptance driver. Package resources are explicit. The canonical GUI SVG is rendered
to an ICO only in external output; no generated ICO binary was added to Git.

[Build script](../tools/build_stage45d.py) refuses existing/output-in-checkout
locations, verifies pinned dependencies/compiler, fixes ZIP timestamps, collects
notices and writes relative inventories. Inputs include application/resources,
packaging/tooling, launchers, metadata, generic template and shipped guide; unrelated
reports are excluded. Source changes reject a candidate. `BUILD_COMPLETE.txt` is
written only after successful artifact generation and final source verification.
Build logs remain local and can contain machine paths; shipped manifest names and
reuse provenance are relative and contain no personal paths. Fixed dates/pins do
not certify byte-identical PE rebuilds.

The full freeze took **303.264 s**. Parent explicitly permitted it after source GUI
acceptance 6/6 PASS at actual DPR 1/1.25/1.5 with restart. Acceptance module SHA256:
`bf663c052c5fc506845ae431b2b1292994f3de9fb1cdbb929f947f1784e2104c`.

The installer-only correction used the narrow optional
`--repackage-frozen-from` path; default behavior still performs a full freeze.
It verified **81 frozen inputs** (all application/resources, spec, GUI/CLI launchers,
project metadata) against the prior manifest, verified **169 EXE/runtime files**
by size and SHA256, copied only the two EXEs and `_internal`, and verified the copy.
Notices, external icon, template, guide, manifest, ZIP and installer were regenerated
in a fresh directory. Prior manifest SHA256:
`f6da5ee7ac931aec12440294264b509c57018179a77e5eabfd61506f611d667f`.
The original and corrected EXEs are byte-identical:

| EXE | SHA256 |
| --- | --- |
| GUI | `20dc29a6fe8b270f5074ffe5163df17916d0ae01f2262691c59374f053ab5984` |
| CLI | `a3170b5ac1d1e3789614d2eb60fffc5d8ba4c3b38f210a3b1602b5189daa5e0e` |

Corrected input capture: **2026-11-26T06:47:17.588506+00:00**. Final verification:
**2026-11-26T06:51:15.037958+00:00**, matched. Corrected Inno compilation:
**191.313 s**. Focused tests reject changed application source and corrupted runtime
reuse. Earlier diagnostic candidates were rejected/failed; none substitutes for
these final artifacts.

## Actual installer validation and correction history

[Inno](../packaging/installer.iss) uses `PrivilegesRequired=lowest`, a per-user
Programs default, Start Menu entries and uninstall metadata. Setup/uninstall refuse
both `Local\GNSSLogger.GUI` and `Local\GNSSLogger.CLI`; AppMutex matches. Parent
GUI and frozen CLI hold their markers until exit. There is no force-close/restart,
InstallDelete or UninstallDelete path. Data/preferences are never uninstall targets.

| Attempt | Actual result |
| --- | --- |
| Initial full-freeze installer | Install 0, version upgrade 0; GUI-mutex uninstall timed out after 60 s on nonsuppressible MsgBox. Continuation later timed out at CLI. Unattended validation FAIL. |
| Corrected installer | Install 0 → registered 0.5.0.45; upgrade 0 → registered 0.5.0.46; uninstall 0. GUI/CLI setup refusals 1 each; GUI/CLI uninstall refusals 1 each. PASS without helpers. |

[Original failure evidence](../../../reports/stage45d-lifecycle-20261126/) is retained.
“User chose OK” entries have unverified origin; scoped WM_COMMAND helpers confirmed
**zero clicks**. After the remaining uninstaller exited, clean uninstall returned 0,
removed executables and preserved all external/unknown-AppRoot sentinels:
[cleanup](../../../reports/stage45d-lifecycle-20261126/cleanup-final-validation.json).
No application was killed to perform cleanup.

The correction uses `SuppressibleMsgBox(..., IDOK)` for setup/uninstall warnings.
All four corrected refusal logs explicitly state **“Defaulting to OK for suppressed
message box”**; no dialog helper was running for the corrected validation.

[Validator](../packaging/validate_install.py) compiles two actual installer versions
with one random test-only AppId, checks both HKCU/HKLM registry views for collisions,
and uses a fresh install directory. It now defines a unique compile-time AppGroup:
`/GROUP` was ineffective with `DisableProgramGroupPage=yes`. The old test-created
links had creation timestamps matching that test install and were removed by its
uninstall. Corrected validation inspected the actual unique test group, found exactly
the two expected links and confirmed its removal. Production shortcut inventories
were **empty before and after**; a pre-existing nonempty production group was not
exercised. Final read-only checks found no test registration/group.

Synthetic preferences, TOML, UBX, RINEX, internal-run and archive sentinels outside
AppRoot retained identical SHA256 before and after install/upgrade/uninstall. The
unknown AppRoot file also survived. The .45/.46 packages test real installer version
upgrade behavior with identical application payload; no new application release
version is claimed.

## Verification

| Check | Observed result |
| --- | --- |
| Focused packaging tests | 6/6 PASS, 0.098 s; no full suite run by this task |
| Diagnostics in new build Python files | No errors |
| Portable integrity | 215 files, all 214 manifest entries match size/SHA256, convbin absent |
| Notices/source collection | 42 files; licensing review remains open |
| Relocated corrected ZIP CLI | PASS, help exit 0, foreign cwd, PATH only System32, PYTHONHOME/PYTHONPATH removed |
| Installed CLI | Help exit 0 with Python removed from PATH |
| Actual corrected setup lifecycle | PASS without helpers; both application mutexes safely refuse replacement/removal |
| Frozen native GUI matrix | PASS, 6/6 write/restart processes, actual DPR 1/1.25/1.5, 46.758 s; foreign Cyrillic cwd, Windows-only PATH, no Python overrides, zero COM/Popen, all four coordinators shut down |
| Extra frozen synthetic recorder | PASS, exit 0, 6 RAWX epochs, STOPPED_COMPLETE; 978 received/written/synced, 0 pending/unarchived |

[Corrected portable CLI evidence](../../../reports/stage45d%20corrected%20portable%20moved/cli-validation.json)
uses an independently extracted ZIP in a moved directory with spaces. Parent
reported its own full suite **443 tests, 442 PASS, 0 FAIL, 0 ERROR, 1 known SKIP,
173.111 s**; its preceding failure was fixed in tests only, leaving frozen inputs
unchanged.

The now-invalidated prior frozen results were read from the parent's
[aggregate JSON](../../../../Temp/vscode-agent-codex-sandbox-eEOMSw/gnss-stage45cd-frozen-60e22b47571644918e0237004714cf4f/aggregate.json)
and [recorder JSON](../../../../Temp/vscode-agent-codex-sandbox-eEOMSw/gnss-stage45cd-frozen-60e22b47571644918e0237004714cf4f/recorder-smoke.json).
Those reported launches used the corrected relocated portable, a foreign Cyrillic
working directory and Windows-only PATH, without Python overrides. Each returned
0, reported zero serial opens/Popen calls and confirmed all four coordinators shut
down. These records no longer establish exact-DPR acceptance. The separate synthetic recorder completed the real recorder lifecycle with
injected data; these are not hardware observations or scientific-quality results.

## Notices and release gates

**Private local evaluation only. Public release remains blocked. No project
license was selected.** Installed-wheel license/copying/notice files, Python runtime
license, Inno license and available upstream pySerial/Qt texts were collected.
[Notice sources](../packaging/notices/SOURCES.md) record upstream URLs. The installed
Qt binding wheels expose commercial-license notices; collecting those and upstream
LGPL/GPL texts is not selection of terms or compliance approval. Complete Qt/module/
third-party attribution and corresponding-source obligations require release review.
Onedir keeps dynamic Qt libraries replaceable.

convbin is **excluded** because exact provenance is unverified. No actual converter
or hardware was used. Signing, SmartScreen/antivirus and **clean Windows 10/11 VM
without Python acceptance were not performed**. Hardware acceptance and scientific
output validation are outside scope. No commit or push was made.
