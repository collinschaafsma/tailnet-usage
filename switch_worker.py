"""SSH stdin helper. Prints status only; credentials never enter command arguments."""
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def is_work_session(name, args):
    """Native Claude also runs its Chrome connector and search tool binary."""
    argv0 = Path(args[0].decode(errors='replace')).name if args else ''
    if argv0 in ('ugrep', 'rg', 'ripgrep'):
        return False
    if len(args) > 1 and args[1] == b'--chrome-native-host':
        return False
    if name.startswith('claude'):
        return True
    return name in ('node', 'bun') and any(b'claude-code' in arg for arg in args[:3])


def active_claude():
    """An open idle work CLI counts as active; non-session helpers do not."""
    if sys.platform == 'darwin':
        result = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=10, check=True)
        count = 0
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid, command = parts
            name = Path(command).name
            if not (name.startswith('claude') or '/claude/' in command or name in ('node', 'bun')):
                continue
            detail = subprocess.run(['ps', '-p', pid, '-o', 'args='], capture_output=True, text=True, timeout=10)
            if detail.returncode:
                continue  # Process exited between snapshots.
            try:
                args = [arg.encode() for arg in shlex.split(detail.stdout)]
            except ValueError:
                count += 1  # Unparseable candidate fails closed.
                continue
            count += is_work_session('claude' if '/claude/' in command else name, args)
        return count
    count = 0
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            name = (path / 'comm').read_text().strip()
            if name.startswith('claude') or name in ('node', 'bun'):
                args = (path / 'cmdline').read_bytes().split(b'\0')
                count += is_work_session(name, args)
        except FileNotFoundError:
            continue
        except PermissionError:
            continue  # Other users' processes cannot use this user's login.
    return count


def keychain_read():
    result = subprocess.run(['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                            capture_output=True, text=True, timeout=10)
    if result.returncode == 44:
        return None
    if result.returncode:
        raise RuntimeError('macOS Keychain unavailable; unlock it before switching')
    return json.loads(result.stdout)


def keychain_write(document):
    # security -i reads the password from stdin; it is never a process argument.
    metadata = subprocess.run(['security', 'find-generic-password', '-s', 'Claude Code-credentials'],
                              capture_output=True, text=True, timeout=10)
    match = re.search(r'"acct"<blob>="([^"]+)"', metadata.stdout + metadata.stderr)
    account = match.group(1) if match else 'Claude Code'
    command = shlex.join(['add-generic-password', '-U', '-a', account,
                         '-s', 'Claude Code-credentials', '-w', json.dumps(document)]) + '\n'
    result = subprocess.run(['security', '-i'], input=command, capture_output=True, text=True, timeout=15)
    # The interactive security process may exit 0 after a command failed.
    if result.returncode or 'SecKeychain' in result.stderr or 'Error:' in result.stderr:
        raise RuntimeError('macOS Keychain could not save the account; unlock it and retry')
    checked = keychain_read()
    if checked != document:
        raise RuntimeError('macOS Keychain write could not be verified')


def operate(request, home=None, process_counter=active_claude, platform=None):
    home = Path(home or Path.home())
    platform = platform or sys.platform
    directory = home / '.claude'
    state_root = home / '.local/state/tailnet-usage/switching'
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    fd = os.open(state_root / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        config_path = directory / '.config.json' if (directory / '.config.json').exists() else home / '.claude.json'
        credential_path = directory / '.credentials.json'
        if config_path.is_symlink() or credential_path.is_symlink():
            raise RuntimeError('Custom symlinked login files require manual switching')
        config = read(config_path)
        original_config = copy.deepcopy(config)
        identity = config.get('oauthAccount', {}).get('emailAddress')
        active = process_counter()
        if request.get('action') == 'probe':
            return {'status': 'ready' if active == 0 else 'waiting', 'active_sessions': active, 'account': identity, 'managed_request': read(state_root / 'managed.json').get('request_id')}
        action = request.get('action')
        if action not in ('apply', 'sync'):
            raise ValueError('Unsupported switch operation')
        target = request.get('target')
        if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', target):
            raise ValueError('Invalid target account')
        payload = request.get('login') or {}
        credentials, metadata = payload.get('credentials') or {}, payload.get('identity') or {}
        if credentials.get('refreshToken') or metadata.get('emailAddress', '').lower() != target.lower():
            raise ValueError('Only a matching access-only login may be applied')
        if not credentials.get('accessToken') or credentials.get('expiresAt', 0) < (time.time() + 300) * 1000:
            raise ValueError('Target access token needs renewal before switching')
        marker = read(state_root / 'managed.json')
        if action == 'sync':
            if marker.get('account') != target or identity != target:
                return {'status': 'conflict', 'message': 'Machine login changed outside the dashboard; automatic token sync stopped'}
        else:
            if active:
                return {'status': 'waiting', 'active_sessions': active, 'account': identity}
            if identity != request.get('expected_current'):
                return {'status': 'conflict', 'message': 'Machine account changed; reload recommendations before switching'}
        original_credentials = keychain_read() if platform == 'darwin' else read(credential_path)
        original_credentials = original_credentials or read(credential_path)
        if action == 'sync':
            old = original_credentials.get('claudeAiOauth', {})
            if old.get('refreshToken'):
                return {'status': 'conflict', 'message': 'Native login replaced the managed login; token sync stopped'}
            if old.get('accessToken') == credentials['accessToken']:
                return {'status': 'synced', 'account': target, 'expires_at': credentials['expiresAt'] / 1000}
        else:
            # Back up only once per request; never overwrite the original on retry.
            identifier = request.get('request_id', '')
            if not re.fullmatch(r'[a-f0-9]{32}', identifier):
                raise ValueError('Invalid switch request ID')
            backup = state_root / ('backup-' + identifier + '.json')
            if not backup.exists():
                write(backup, {'credentials': original_credentials, 'identity': config.get('oauthAccount'),
                               'created_at': time.time()})
            active = process_counter()
            if active:
                return {'status': 'waiting', 'active_sessions': active, 'account': identity}
        installed = dict(original_credentials)
        installed['claudeAiOauth'] = {key: credentials[key] for key in (
            'accessToken', 'expiresAt', 'scopes', 'subscriptionType', 'rateLimitTier') if key in credentials}
        try:
            if platform == 'darwin':
                keychain_write(installed)
            write(credential_path, installed)
            if action == 'apply':
                config['oauthAccount'] = metadata
                write(config_path, config)
                write(state_root / 'managed.json', {'account': target, 'request_id': request['request_id'], 'applied_at': time.time()})
        except Exception:
            # Restore locally; never return either credential set to the caller.
            if platform == 'darwin':
                keychain_write(original_credentials)
            write(credential_path, original_credentials)
            write(config_path, original_config)
            write(state_root / 'managed.json', marker)
            raise RuntimeError('Account write failed; original login was restored') from None
        return {'status': 'applied' if action == 'apply' else 'synced', 'account': target,
                'expires_at': credentials['expiresAt'] / 1000, 'active_sessions': active}
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        print(json.dumps(operate(json.load(sys.stdin))))
    except Exception as error:
        # Do not serialize raw exceptions: file/Keychain errors can contain secrets.
        print(json.dumps({'status': 'error', 'message': 'Switch could not be completed; verify machine access and unlock Keychain on macOS'}))
