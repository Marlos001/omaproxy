"""Reviewed backend updates. This module never trusts a remotely discovered pin."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error

METADATA_MAX_BYTES = 512 * 1024
CONFIG_MAX_BYTES = 2 * 1024 * 1024
PROBE_MAX_BYTES = 128 * 1024
VERSION_PATTERN = r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?"


def normalized_version(value):
    return 'v' + value.lstrip('v') if isinstance(value, str) and re.fullmatch(VERSION_PATTERN, value) else ''


def is_newer(candidate, installed):
    try:
        return tuple(map(int, candidate.lstrip('v').split('.'))) > tuple(map(int, installed.lstrip('v').split('.')))
    except (ValueError, AttributeError):
        return False


def probe(binary):
    """Execute help in an empty directory, with no user configuration/environment."""
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as output:
        try:
            subprocess.run([str(binary), '--help'], cwd=directory,
                           env={'PATH': '/usr/bin:/bin', 'HOME': directory},
                           stdout=output, stderr=output, timeout=8, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Backend executable probe failed; no changes made.') from None
        if output.tell() > PROBE_MAX_BYTES:
            raise ValueError('Backend help exceeds the probe size limit.')
        output.seek(0)
        text = output.read(PROBE_MAX_BYTES).decode('utf-8', errors='replace')
    match = re.search(r'CLIProxyAPI Version:\s*(' + VERSION_PATTERN + r')(?:,|\s|$)', text)
    if not match:
        raise ValueError('Backend executable did not report a recognized version.')
    flags = set(re.findall(r'^\s+--?([a-z][a-z-]+)(?:\s|$)', text, re.M))
    return {'version': normalized_version(match.group(1)), 'flags': flags}


def provider_capabilities(bridge, info):
    return [{'id': ident, 'name': name, 'flag': flag, 'available': flag in info['flags']}
            for ident, name, flag in bridge.PROVIDERS]


def update_supported(bridge):
    return (bridge.platform.system() == 'Linux' and bridge.platform.machine() in ('x86_64', 'aarch64')
            and bool(shutil.which('bwrap')))


def running_version(request, cfg):
    headers = {}
    try:
        request(f'http://127.0.0.1:{cfg["port"]}/v0/management/auth-files',
                cfg['management_key'], timeout=2, response_headers=headers)
        return normalized_version(next((value for key, value in headers.items()
                                         if key.lower() == 'x-cpa-version'), ''))
    except (OSError, ValueError, urllib.error.URLError):
        return ''


def managed_binary(bridge, cfg):
    target = bridge.DATA / 'cli-proxy-api'
    if (not cfg or cfg.get('version') == 'custom' or cfg.get('binary') != str(target)
            or target.is_symlink() or not target.is_file()):
        raise ValueError('Automatic updates require the OmaProxy-managed backend. Custom executables must be updated separately.')
    return target


def check_updates(bridge):
    cfg = bridge.settings()
    result = {'reviewed_version': bridge.VERSION, 'latest_version': '', 'installed_version': '',
              'version_source': '', 'update_available': False, 'update_supported': False,
              'rollback_available': False, 'providers': [], 'error': ''}
    if cfg:
        try:
            target = managed_binary(bridge, cfg)
            info = probe(target)
            result.update(installed_version=info['version'], version_source='executable',
                          update_supported=update_supported(bridge),
                          providers=provider_capabilities(bridge, info))
            if not result['update_supported']:
                result['error'] = 'Safe updates require Linux x86_64/aarch64 and bubblewrap (bwrap).'
            result['rollback_available'] = (bridge.DATA / 'backend-backup' / 'receipt.json').is_file()
        except ValueError as exc:
            result['error'] = str(exc)
        if bridge.systemctl('is-active', check=False).stdout.strip() == 'active':
            observed = running_version(bridge.request, cfg)
            if observed:
                if result['installed_version'] and observed != result['installed_version']:
                    result['error'] = ('Running and installed backend versions differ. Stop the proxy before updating, '
                                       'or restart it to use the installed executable.')
                result.update(installed_version=observed, version_source='running')
    try:
        metadata = json.loads(bridge.download(f'https://api.github.com/repos/{bridge.REPO}/releases/latest', METADATA_MAX_BYTES))
        if not isinstance(metadata, dict):
            raise ValueError('The release server returned invalid release metadata.')
        latest = normalized_version(metadata.get('tag_name'))
        if not latest or metadata.get('draft') or metadata.get('prerelease'):
            raise ValueError('The release server returned invalid release metadata.')
        result['latest_version'] = latest
    except (OSError, ValueError, urllib.error.URLError):
        result['error'] = result['error'] or 'Latest release could not be checked. The reviewed release remains available.'
    result['update_available'] = is_newer(bridge.VERSION, result['installed_version'])
    return {'updates': result}


# Runs inside a fresh network namespace. stdout contains a fixed receipt only;
# backend logs go to /dev/null and credentials arrive over stdin, never argv.
_SANDBOX_PROBE = r'''
import json, subprocess, sys, time, urllib.request
cfg = json.load(sys.stdin)
args = ['/backend', '--config', '/validation/config.yaml']
if cfg['local_model']: args += ['-local-model']
p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and p.poll() is None:
        try:
            for route, key, field in [('v0/management/auth-files', cfg['management_key'], 'files'), ('v1/models', cfg['api_key'], 'data')]:
                req = urllib.request.Request('http://127.0.0.1:%s/%s' % (cfg['port'], route), headers={'Authorization': 'Bearer ' + key})
                with opener.open(req, timeout=.4) as response:
                    if field not in json.load(response): raise ValueError()
                    version = response.headers.get('X-CPA-VERSION', '')
                    if route.startswith('v0/') and version.lstrip('v') != cfg['version'].lstrip('v'): raise ValueError()
            print('{"validated":true}')
            break
        except Exception:
            time.sleep(.1)
    else: sys.exit(1)
finally:
    p.terminate()
    try: p.wait(timeout=2)
    except subprocess.TimeoutExpired: p.kill(); p.wait()
'''


def validate_config(bridge, binary, cfg, info, directory):
    """Ask the actual backend to parse the unchanged legacy config in isolation."""
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise ValueError('Safe backend validation requires bubblewrap (bwrap). Install it before updating.')
    source = bridge.CONFIG / 'config.yaml'
    if not source.is_file() or source.stat().st_size > CONFIG_MAX_BYTES:
        raise ValueError('Backend configuration is missing or exceeds the validation size limit.')
    validation = Path(directory) / 'validation'
    validation.mkdir(mode=0o700)
    (validation / 'config.yaml').write_bytes(source.read_bytes())
    (validation / 'config.yaml').chmod(0o600)
    # The namespace has only executable/runtime files and a private config copy.
    # Real HOME, credentials, service sockets and proxy environment are absent.
    args = [bwrap, '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib',
            '--symlink', 'usr/lib', '/lib64', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--tmpfs', '/home', '--dir', '/root',
            '--ro-bind', str(binary), '/backend', '--bind', str(validation), '/validation',
            '--clearenv', '--setenv', 'HOME', '/home', '--chdir', '/validation',
            '--', '/usr/bin/python3', '-c', _SANDBOX_PROBE]
    payload = {key: cfg[key] for key in ('port', 'api_key', 'management_key')}
    payload.update(version=info['version'], local_model='local-model' in info['flags'])
    try:
        result = subprocess.run(args, input=json.dumps(payload), text=True, capture_output=True,
                                timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Isolated backend validation failed; no changes made.') from None
    if result.returncode or result.stdout.strip() != '{"validated":true}':
        raise ValueError('The new backend could not validate this configuration in isolation. No changes made.')


def _private_copy(source, target, executable=False):
    shutil.copyfile(source, target)
    target.chmod(0o700 if executable else 0o600)


def _replace_copy(source, target, executable=False):
    fd, name = tempfile.mkstemp(dir=target.parent)
    os.close(fd)
    staged = Path(name)
    try:
        _private_copy(source, staged, executable)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _wait_running(bridge, cfg, expected):
    for _ in range(20):
        if (bridge.systemctl('is-active', check=False).stdout.strip() == 'active'
                and running_version(bridge.request, cfg) == expected):
            bridge.request(f'http://127.0.0.1:{cfg["port"]}/v1/models', cfg['api_key'])
            return
        time.sleep(.25)
    raise ValueError('Updated service did not become healthy; restoring the previous backend.')


def _recover_pending(bridge):
    """A killed updater leaves a private snapshot; the next explicit action restores it."""
    pending = bridge.DATA / 'backend-pending'
    if not pending.exists():
        return
    try:
        receipt = json.loads((pending / 'receipt.json').read_text())
        cfg = json.loads((pending / 'settings.json').read_text())
        target = managed_binary(bridge, cfg)
        if (pending / 'cli-proxy-api').stat().st_size > bridge.BINARY_MAX_BYTES:
            raise ValueError()
        if hashlib.sha256((pending / 'cli-proxy-api').read_bytes()).hexdigest() != receipt['sha256']:
            raise ValueError()
    except (OSError, ValueError, KeyError):
        raise ValueError('Interrupted update backup is invalid. Restore the backend manually before updating.') from None
    try:
        bridge.systemctl('stop', check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    _replace_copy(pending / 'cli-proxy-api', target, True)
    for name in ('settings.json', 'config.yaml'):
        _replace_copy(pending / name, bridge.CONFIG / name)
    if receipt.get('running'):
        bridge.systemctl('start')
        _wait_running(bridge, cfg, receipt['version'])
    shutil.rmtree(pending)


def change_backend(bridge, rollback=False):
    bridge.CONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (bridge.CONFIG / '.backend-update.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another backend update is in progress.') from None
        _recover_pending(bridge)
        cfg = bridge.settings()
        target = managed_binary(bridge, cfg)
        old_info = probe(target)
        if not rollback and old_info['version'] == bridge.VERSION:
            if bridge.systemctl('is-active', check=False).stdout.strip() == 'active':
                _require_matching_running_version(bridge, cfg, old_info['version'])
            refreshed = dict(cfg, version=old_info['version'], providers=provider_capabilities(bridge, old_info))
            if refreshed != cfg:
                bridge.private_write(bridge.CONFIG / 'settings.json', json.dumps(refreshed, indent=2) + '\n')
            return _receipt(bridge, old_info, False, 'The reviewed backend is already installed.')
        if not rollback and not is_newer(bridge.VERSION, old_info['version']):
            raise ValueError('The installed backend is newer than the reviewed release. Automatic downgrade refused.')
        if not update_supported(bridge):
            raise ValueError('Safe updates require Linux x86_64/aarch64 and bubblewrap (bwrap).')
        bridge.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = bridge.DATA / 'backend-backup'
        with tempfile.TemporaryDirectory(dir=bridge.DATA, prefix='.backend-update-') as temporary:
            directory = Path(temporary)
            candidate = directory / 'cli-proxy-api'
            candidate_cfg = dict(cfg)
            if rollback:
                try:
                    if (backup / 'cli-proxy-api').stat().st_size > bridge.BINARY_MAX_BYTES:
                        raise ValueError()
                    receipt = json.loads((backup / 'receipt.json').read_text())
                    if hashlib.sha256((backup / 'cli-proxy-api').read_bytes()).hexdigest() != receipt['sha256']:
                        raise ValueError()
                    _private_copy(backup / 'cli-proxy-api', candidate, True)
                    candidate_cfg = json.loads((backup / 'settings.json').read_text())
                except (OSError, ValueError, KeyError):
                    raise ValueError('A valid private rollback backup is not available.') from None
                managed_binary(bridge, candidate_cfg)
            else:
                bridge.install_binary(candidate)
            info = probe(candidate)
            if not rollback and info['version'] != bridge.VERSION:
                raise ValueError('Downloaded backend version does not match the reviewed release.')
            # Rollback validates its preserved config, rather than the current config.
            validation_bridge = bridge
            if rollback:
                validation_bridge = type('ValidationBridge', (), {'CONFIG': backup})
            validate_config(validation_bridge, candidate, candidate_cfg, info, directory)
            # Publish a complete private snapshot before touching the service or binary.
            staged_snapshot = directory / 'previous'
            staged_snapshot.mkdir(mode=0o700)
            for name, source in [('cli-proxy-api', target), ('settings.json', bridge.CONFIG / 'settings.json'),
                                 ('config.yaml', bridge.CONFIG / 'config.yaml')]:
                _private_copy(source, staged_snapshot / name, name == 'cli-proxy-api')
            candidate_cfg.update(version=info['version'], providers=provider_capabilities(bridge, info))
            running = bridge.systemctl('is-active', check=False).stdout.strip() == 'active'
            if running:
                _require_matching_running_version(bridge, cfg, old_info['version'])
            receipt = {'version': old_info['version'], 'running': running,
                       'sha256': hashlib.sha256((staged_snapshot / 'cli-proxy-api').read_bytes()).hexdigest()}
            bridge.private_write(staged_snapshot / 'receipt.json', json.dumps(receipt) + '\n')
            snapshot = bridge.DATA / 'backend-pending'
            os.replace(staged_snapshot, snapshot)
            changed = False
            try:
                if running:
                    bridge.systemctl('stop')
                changed = True
                os.replace(candidate, target)
                if rollback:
                    _replace_copy(backup / 'config.yaml', bridge.CONFIG / 'config.yaml')
                bridge.private_write(bridge.CONFIG / 'settings.json', json.dumps(candidate_cfg, indent=2) + '\n')
                if running:
                    bridge.systemctl('start')
                    _wait_running(bridge, candidate_cfg, info['version'])
                # Keep the last working state private. A completed rollback can be reversed.
                old_backup = directory / 'old-backup'
                if backup.exists():
                    os.replace(backup, old_backup)
                os.replace(snapshot, backup)
            except BaseException:
                if 'old_backup' in locals() and old_backup.exists() and not backup.exists():
                    os.replace(old_backup, backup)
                if changed:
                    if running:
                        try:
                            bridge.systemctl('stop', check=False)
                        except (OSError, subprocess.SubprocessError):
                            pass
                    _replace_copy(snapshot / 'cli-proxy-api', target, True)
                    for name in ('settings.json', 'config.yaml'):
                        _replace_copy(snapshot / name, bridge.CONFIG / name)
                if running:
                    try:
                        bridge.systemctl('start')
                        _wait_running(bridge, cfg, old_info['version'])
                    except (OSError, ValueError, subprocess.SubprocessError):
                        raise ValueError('Previous backend and configuration restored, but the service could not restart. Open Logs.') from None
                shutil.rmtree(snapshot)
                raise
        return _receipt(bridge, info, running, 'Backend rolled back.' if rollback else 'Backend updated.')


def _require_matching_running_version(bridge, cfg, installed):
    observed = running_version(bridge.request, cfg)
    if not observed:
        raise ValueError('The active backend version could not be verified. Stop the proxy before updating.')
    if observed != installed:
        # The old running executable has already been replaced on disk. A backup
        # of the disk file cannot restore that process after a failed restart.
        raise ValueError(f'The active backend reports {observed}, but the installed executable reports {installed}. '
                         'Stop the proxy before updating, or restart it to use the installed executable.')


def _receipt(bridge, info, restarted, message):
    return {'message': message, 'updates': {'installed_version': info['version'], 'latest_version': '',
            'reviewed_version': bridge.VERSION, 'version_source': 'executable',
            'update_available': is_newer(bridge.VERSION, info['version']),
            'update_supported': update_supported(bridge), 'rollback_available':
            (bridge.DATA / 'backend-backup' / 'receipt.json').is_file(),
            'providers': provider_capabilities(bridge, info), 'restarted': restarted, 'error': ''}}
