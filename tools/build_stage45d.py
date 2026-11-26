"""Private evaluation build. Output must be outside the checkout; never bundles convbin."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as md
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import struct
import sys
import zipfile

REPO = Path(__file__).resolve().parents[1]
PINS = {'pyinstaller': '6.22.3', 'PySide6-Essentials': '6.12.0', 'shiboken6': '6.12.0', 'pyserial': '3.5',
        'altgraph': '0.17.5', 'packaging': '26.4', 'pefile': '2024.8.26',
        'pyinstaller-hooks-contrib': '2026.10', 'pywin32-ctypes': '0.2.3', 'setuptools': '84.2.1'}

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def inventory(root):
    return {p.relative_to(root).as_posix(): {'sha256': sha(p), 'bytes': p.stat().st_size}
            for p in sorted(root.rglob('*')) if p.is_file()}

def make_icon(svg, target):
    """Render the canonical SVG; generated ICO stays outside the checkout."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise ValueError('Invalid application SVG')
    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise RuntimeError('PNG icon encoding failed')
    png = bytes(buffer.data())
    buffer.close()
    target.write_bytes(struct.pack('<HHH', 0, 1, 1) +
                       struct.pack('<BBBBHHII', 64, 64, 0, 0, 1, 32, len(png), 22) + png)

def sources():
    paths = [p for folder in ('gnss_logger', 'packaging')
             for p in sorted((REPO/folder).rglob('*'))
             if p.is_file() and '__pycache__' not in p.parts]
    paths.extend(REPO/name for name in ('pyproject.toml', 'tools/build_stage45d.py',
                 'tools/launch_gui_stage4_2.py', 'config/conversion.template.toml', 'docs/user_guide_ru.md'))
    return {p.relative_to(REPO).as_posix(): sha(p) for p in paths}

def is_freeze_input(name):
    return name.startswith('gnss_logger/') or name in (
        'packaging/gnss.spec', 'packaging/launch_cli.py',
        'tools/launch_gui_stage4_2.py', 'pyproject.toml')

def verify_freeze_inputs(prior):
    previous = {k: v for k, v in prior['source_sha256'].items() if is_freeze_input(k)}
    current = {k: v for k, v in sources().items() if is_freeze_input(k)}
    if previous != current:
        raise RuntimeError('Frozen inputs changed; full freeze required')
    return current

def reuse_frozen(prior_root, payload):
    """Reuse only a completed, source-identical and hash-verified frozen runtime."""
    if not (prior_root/'BUILD_COMPLETE.txt').is_file():
        raise RuntimeError('Prior build is not complete')
    prior_payload = prior_root/'dist/GNSSLogger'
    manifest_path = prior_payload/'build-manifest.json'
    prior = json.loads(manifest_path.read_text(encoding='utf-8'))
    verified_inputs = verify_freeze_inputs(prior)
    def frozen(name):
        return name in ('gnss-logger.exe', 'gnss-logger-gui.exe') or name.startswith('_internal/')
    expected = {k: v for k, v in prior['files'].items() if frozen(k)}
    actual = {k: v for k, v in inventory(prior_payload).items() if frozen(k)}
    if actual != expected or not all(name in expected for name in ('gnss-logger.exe', 'gnss-logger-gui.exe')):
        raise RuntimeError('Prior frozen payload hash/size mismatch')
    payload.mkdir(parents=True, exist_ok=False)
    for name in ('gnss-logger.exe', 'gnss-logger-gui.exe'):
        shutil.copyfile(prior_payload/name, payload/name)
    shutil.copytree(prior_payload/'_internal', payload/'_internal')
    if inventory(payload) != expected:
        raise RuntimeError('Copied frozen payload hash/size mismatch')
    return {'reused': True, 'prior_manifest_sha256': sha(manifest_path),
            'verified_freeze_inputs': verified_inputs, 'verified_frozen_files': expected}

def run(command, log, env=None):
    with log.open('w', encoding='utf-8') as stream:
        subprocess.run(list(map(str, command)), cwd=log.parent, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)

def verify_sources(before, output):
    after = sources()
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    (output/'source-verification.json').write_text(json.dumps({
        'checked_utc': datetime.now(timezone.utc).isoformat(), 'matched': not changed,
        'changed_files': changed, 'files': after}, indent=2), encoding='utf-8')
    if changed:
        raise RuntimeError('Source changed during build; reject this artifact: '+', '.join(changed))

def notices(payload):
    folder = payload/'licenses'
    folder.mkdir()
    shutil.copytree(REPO/'packaging/notices', folder/'upstream-notices')
    components = []
    for name in PINS:
        dist = md.distribution(name)
        for item in dist.files or []:
            if any(token in str(item).lower() for token in ('license', 'copying', 'notice')):
                src = Path(dist.locate_file(item))
                if src.is_file():
                    dest = folder/name/str(item).replace('../', '')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)
        components.append({'name': name, 'version': dist.version,
                           'project_urls': dist.metadata.get_all('Project-URL', [])})
    license_file = Path(sys.base_prefix)/'LICENSE.txt'
    if license_file.is_file():
        shutil.copyfile(license_file, folder/'Python-LICENSE.txt')
    (folder/'SOURCE-INSTRUCTIONS.txt').write_text(
        'PRIVATE LOCAL EVALUATION ONLY. Project license not selected; public release blocked.\n'
        'Python: https://www.python.org/downloads/source/\n'
        'Qt/PySide/shiboken 6.12.0: https://download.qt.io/official_releases/QtForPython/\n'
        'Qt sources: https://download.qt.io/official_releases/qt/6.12/\n'
        'Qt licensing: https://doc.qt.io/qtforpython-6/licenses.html\n'
        'Dynamic Qt DLLs in _internal may be replaced with compatible builds.\n'
        'pySerial 3.5: https://github.com/pyserial/pyserial/tree/v3.5\n'
        'PyInstaller: https://github.com/pyinstaller/pyinstaller/tree/v6.22.3\n'
        'Inno Setup: https://github.com/jrsoftware/issrc/tree/is-6_7_4\n'
        'Source availability and complete Qt third-party compliance require release review.\n'
        'convbin excluded: exact binary provenance unverified.\n', encoding='utf-8')
    return components

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--iscc', required=True, type=Path)
    parser.add_argument('--version', default='0.5.0.45')
    parser.add_argument('--repackage-frozen-from', type=Path,
                        help='Reuse completed frozen EXEs/runtime only after exact input and payload verification')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(REPO):
        parser.error('output must be outside checkout')
    if platform.python_version() != '3.15.1':
        parser.error('requires Python 3.15.1')
    for name, version in PINS.items():
        if md.version(name) != version:
            parser.error(f'{name} must be {version}')
    if sha(args.iscc) != 'f02fa0e5f237b47d7ed1edae9b991866ec94fe02af61eb6a73e94f92fe8e0604':
        parser.error('ISCC differs from verified official 6.7.4 compiler')
    output.mkdir(parents=True, exist_ok=False)
    before = sources()
    captured = datetime.now(timezone.utc).isoformat()
    (output/'source-snapshot.json').write_text(json.dumps({'captured_utc': captured, 'files': before}, indent=2), encoding='utf-8')
    icon = output/'build-icon.ico'
    make_icon(REPO/'gnss_logger/gui/resources/gnss-logger.svg', icon)
    env = dict(os.environ, GNSS_BUILD_SOURCE=str(REPO), GNSS_BUILD_ICON=str(icon), PYTHONHASHSEED='0', SOURCE_DATE_EPOCH='1795651200')
    payload = output/'dist/GNSSLogger'
    reuse = {'reused': False}
    if args.repackage_frozen_from:
        reuse = reuse_frozen(args.repackage_frozen_from.resolve(), payload)
        (output/'freeze-reuse.json').write_text(json.dumps(reuse, indent=2), encoding='utf-8')
    else:
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', output/'dist',
             '--workpath', output/'work', REPO/'packaging/gnss.spec'], output/'freeze.log', env)
    verify_sources(before, output)
    (payload/'resources').mkdir()
    shutil.copyfile(icon, payload/'resources/gnss-logger.ico')
    components = notices(payload)
    shutil.copyfile(args.iscc.parent/'License.txt', payload/'licenses/Inno-Setup-LICENSE.txt')
    templates = payload/'config-templates'
    templates.mkdir()
    shutil.copyfile(REPO/'config/conversion.template.toml', templates/'conversion.template.toml')
    (payload/'docs').mkdir()
    shutil.copyfile(REPO/'docs/user_guide_ru.md', payload/'docs/user_guide_ru.md')
    manifest = {'private_evaluation_only': True, 'release_blocked': ['project license', 'dependency compliance', 'clean-machine acceptance'],
                'convbin_included': False, 'python': platform.python_version(), 'architecture': platform.machine(),
                'components': components, 'source_sha256': before,
                'freeze': reuse,
                'build_environment': sorted([{'name': d.metadata['Name'], 'version': d.version} for d in md.distributions()], key=lambda d:d['name']),
                'inno_setup': {'version': '6.7.4', 'compiler_sha256': sha(args.iscc),
                               'installer_sha256': 'a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c',
                               'publisher': 'Pyrsys B.V.', 'authenticode_at_acquisition': 'Valid'},
                'git_head': subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
                'files': inventory(payload)}
    (payload/'build-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    with zipfile.ZipFile(output/'GNSSLogger-portable.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(payload.rglob('*')):
            if path.is_file():
                info = zipfile.ZipInfo('GNSSLogger/'+path.relative_to(payload).as_posix(), (2026,11,26,0,0,0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    run([args.iscc, f'/DPayload={payload}', f'/DBuildOutput={output}', f'/DBuildVersion={args.version}', f'/DBuildIcon={icon}',
         REPO/'packaging/installer.iss'], output/'installer.log')
    verify_sources(before, output)
    (output/'SHA256SUMS.txt').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in sorted(output.iterdir()) if p.suffix in ('.zip', '.exe')), encoding='utf-8')
    (output/'BUILD_COMPLETE.txt').write_text('Private local evaluation only; source snapshots matched.\n', encoding='utf-8')
    print(output)

if __name__ == '__main__':
    main()
