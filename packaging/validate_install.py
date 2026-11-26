"""Actual isolated per-user install/update/uninstall with preservation and mutex checks."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import winreg

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('setup', type=Path)
    parser.add_argument('root', type=Path)
    parser.add_argument('--iscc', required=True, type=Path)
    parser.add_argument('--payload', required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    identity = 'GNSSLogger-Test-' + uuid.uuid4().hex
    registry = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + identity + '_is1'
    programs = Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs'
    group = programs/identity
    assert not group.exists(), 'test shortcut group already exists'
    production_group = programs/'GNSS Logger'
    def shortcut_hashes(folder):
        return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in folder.rglob('*') if p.is_file()}
    production_shortcuts = shortcut_hashes(production_group)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, registry, 0, winreg.KEY_READ | view):
                    raise RuntimeError('Test installer identity already registered')
            except FileNotFoundError:
                pass
    # Compile identical files/settings with a unique identity; never touch production registration.
    for version, filename in (('0.5.0.45', 'isolated-test-setup'), ('0.5.0.46', 'isolated-test-upgrade')):
        with (root/(filename+'-compile.log')).open('w', encoding='utf-8') as log:
            subprocess.run([str(args.iscc.resolve()), f'/DPayload={args.payload.resolve()}',
                            f'/DBuildOutput={root}', f'/DAppIdentity={identity}', f'/DAppGroup={identity}', f'/DBuildVersion={version}',
                            f'/DBuildIcon={args.payload.resolve()}/resources/gnss-logger.ico',
                            '/F'+filename, str(Path(__file__).with_name('installer.iss'))],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    args.setup = root/'isolated-test-setup.exe'
    app = root/'app'
    data = root/'localappdata/GNSS'
    data.mkdir(parents=True)
    sentinels = {}
    for name in ('config/gui-preferences.json', 'conversion.local.toml', 'data/synthetic.ubx', 'data/rinex/synthetic.obs', 'internal/runs/sentinel.txt', 'archives/sentinel.txt'):
        path = data/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'PRIVATE SYNTHETIC SENTINEL\r\n')
        sentinels[path] = path.read_bytes()
    env = dict(os.environ, LOCALAPPDATA=str(root/'localappdata'))
    base = [str(args.setup.resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', f'/DIR={app}', f'/GROUP={identity}']
    results = {'test_app_id': identity, 'test_group': identity,
               'production_shortcuts_before_sha256': production_shortcuts}
    def setup(label):
        command = list(base)
        if label == 'update':
            command[0] = str(root/'isolated-test-upgrade.exe')
        result = subprocess.run(command+[f'/LOG={root/label}.log'], env=env, timeout=180)
        results[label] = result.returncode
        return result.returncode
    def installed_version():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, 'DisplayVersion')[0]
    def preserved(label):
        assert all(p.read_bytes() == value for p, value in sentinels.items()), 'data changed'
        assert shortcut_hashes(production_group) == production_shortcuts, 'production shortcuts changed'
        results[label+'-sentinel-sha256'] = {p.relative_to(data).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sentinels}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    preserved('before')
    for name in ('GUI', 'CLI'):
        handle = kernel.CreateMutexW(None, False, 'Local\\GNSSLogger.'+name)
        assert handle
        try:
            assert setup('active-'+name+'-mutex-rejected') != 0
            assert not app.exists()
        finally:
            kernel.CloseHandle(handle)
    assert setup('install') == 0
    results['installed_version'] = installed_version()
    assert results['installed_version'] == '0.5.0.45'
    assert sorted(p.name for p in group.glob('*.lnk')) == ['GNSS Logger CLI help.lnk', 'GNSS Logger.lnk']
    preserved('install')
    unknown = app/'user-owned-sentinel.txt'
    unknown.write_bytes(b'PRESERVE UNKNOWN APP FILE')
    assert setup('update') == 0
    results['upgraded_version'] = installed_version()
    assert results['upgraded_version'] == '0.5.0.46'
    preserved('update')
    assert unknown.read_bytes() == b'PRESERVE UNKNOWN APP FILE'
    result = subprocess.run([str(app/'gnss-logger.exe'), '--help'], env=dict(env, PATH=os.environ['SystemRoot']+'\\System32'), capture_output=True, timeout=30)
    results['installed-cli'] = result.returncode
    assert result.returncode == 0
    uninstaller = app/'unins000.exe'
    for name in ('GUI', 'CLI'):
        handle = kernel.CreateMutexW(None, False, 'Local\\GNSSLogger.'+name)
        assert handle
        try:
            result = subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/LOG={root}/uninstall-{name}-blocked.log'], env=env, timeout=60)
            assert result.returncode != 0
            assert (app/'gnss-logger.exe').exists()
            results['active-'+name+'-mutex-uninstall-rejected'] = result.returncode
        finally:
            kernel.CloseHandle(handle)
    result = subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/LOG={root}/uninstall.log'], env=env, timeout=120)
    results['uninstall'] = result.returncode
    for _ in range(100):
        if not (app/'gnss-logger.exe').exists():
            break
        time.sleep(.1)
    assert not (app/'gnss-logger.exe').exists()
    assert unknown.read_bytes() == b'PRESERVE UNKNOWN APP FILE'
    preserved('uninstall')
    results['sentinels_unchanged'] = True
    results['production_shortcuts_after_sha256'] = shortcut_hashes(production_group)
    results['test_shortcuts_removed'] = not group.exists()
    assert results['test_shortcuts_removed']
    (root/'validation.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results))

if __name__ == '__main__':
    main()
