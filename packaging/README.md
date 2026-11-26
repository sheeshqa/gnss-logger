# Stage 4.5d private evaluation builds

Use Windows x64 CPython 3.15.1 in a dedicated build environment. Never install build
dependencies into the primary project environment. Install the exact wheels with:

```powershell
& $buildPython -m pip install --require-hashes -r packaging/build-requirements.lock
```

The lock targets Windows x64 wheels, including PySide6 Essentials rather than the
full PySide6 metapackage. Retain downloaded wheels for rebuilding offline. Install
Inno Setup 6.7.4 per-user into a dedicated toolchain directory. Its official link is
listed at <https://jrsoftware.org/isdl.php>, pointing to
<https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe>.
Verify Authenticode status `Valid`, publisher `Pyrsys B.V.`, and SHA256
`a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c`
before execution. No admin or global installation is needed.

After GUI sources are stable, reserve a fresh output outside the checkout:

```powershell
& $buildPython tools/build_stage45d.py --output $freshOutput --iscc $localISCC
& $buildPython -m unittest discover -s packaging -p test_build.py -v
& $buildPython packaging/validate_install.py $setupExe $freshValidationRoot --iscc $localISCC --payload $builtPayload
```

For an installer/tooling-only correction after a completed full freeze, use
`--repackage-frozen-from $priorBuild`. This verifies every application/resource,
spec, CLI/GUI launcher and project-metadata hash against the prior manifest, plus
every EXE/runtime file hash and size. It copies only the two EXEs and `_internal`
to a fresh output, verifies the copy, then regenerates all notices/icon/template/
guide/manifest/ZIP/setup. Changed frozen inputs require a new full freeze. Reuse
provenance is recorded without local paths. The default still performs a full build.

The installer uses suppressible mutex warnings so `/VERYSILENT /SUPPRESSMSGBOXES`
refuses active applications without a blocking dialog. Interactive installs still
explain the refusal. Tests define both a fresh AppId and a fresh compile-time
DefaultGroupName: `/GROUP` is ignored when the program-group page is disabled.
Existing production shortcuts are inventoried and compared throughout validation.

The [spec](gnss.spec) builds a windowed GUI and console CLI in one shared onedir.
Both launchers hold named Windows mutexes; [Inno](installer.iss) refuses updates and
uninstallation while either exists. Close the application normally; the installer
never kills a recording process. CLI source invocations are outside this frozen
launcher contract. Install/update tests run two separately compiled package versions
(0.5.0.45 then 0.5.0.46), verify the registered version, then uninstall. Identical
application payload tests installer upgrade behavior without a new application release.

The build fails if fingerprinted build inputs change while freezing. Inputs include
the package/resources, packaging/tooling, project metadata, launcher, template and
shipped user guide; unrelated reports are excluded. SHA256 manifests
contain relative source and payload names, component versions and hashes. Build
logs remain local and may contain machine paths. The build writes the UTC capture
time and full relative SHA256 map before freezing. The canonical GUI SVG is rendered
to an ICO only in the external output directory, then used by EXEs and Setup.
ZIP timestamps are fixed and
dependencies pinned; byte-identical PE outputs have not been certified.

The installer writes only its AppRoot, shortcuts and per-user uninstall metadata.
No uninstall deletion rules touch data/preferences. Portable data defaults remain
outside AppRoot. The validator recompiles the identical payload with a random test
AppId, checks both registry views for identity collisions, and uses a matching
unique shortcut group and a fresh explicit install directory, synthetic
external preferences/TOML/UBX/RINEX sentinels and an unknown AppRoot sentinel. It
does not delete those remaining sentinels. Inspect its JSON and Inno logs.

Coordinate frozen GUI acceptance with the parent: `--mock --acceptance-smoke PATH
--smoke-images PATH`; run outside AppRoot with Python removed from PATH. Do not
run hardware, real convbin or the full regression suite as part of packaging.

**Private local evaluation only. Public release is blocked.** No project license
has been chosen. Shipped notices and upstream source URLs are collected, but Qt
third-party/source obligations, signing, clean Windows 10/11 machine acceptance and
project licensing require separate release review. convbin is excluded because its
exact provenance is unverified. No EULA restricts replacement of dynamic Qt DLLs.
