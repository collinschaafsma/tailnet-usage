"""Runs locally or over SSH. Credentials never leave the machine being inspected."""
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import selectors
import sqlite3
import shutil
import subprocess
import time
import urllib.request
import urllib.error


def workload():
    """Incrementally index numeric usage only. Never persist conversation text."""
    now = time.time()
    directory = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'tailnet-usage'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = root / 'workload.sqlite3'
    connection = sqlite3.connect(database, timeout=3)
    os.chmod(database, 0o600)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, position INTEGER, modified REAL)')
        connection.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, stamp REAL, input INTEGER, output INTEGER, cached INTEGER, created INTEGER)')
        files = []
        for path in (directory / 'projects').rglob('*.jsonl'):
            try:
                stat = path.stat()
                if stat.st_mtime >= now - 7 * 86400:
                    files.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                continue
        files.sort(key=lambda item: item[2], reverse=True)
        deadline, scanned, consumed = time.monotonic() + 8, 0, 0
        for path, size, modified in files:
            if time.monotonic() >= deadline or consumed >= 64 * 1024 * 1024:
                break
            old = connection.execute('SELECT position, modified FROM files WHERE path=?', (str(path),)).fetchone()
            position = old[0] if old and old[0] <= size else 0
            if old and position == size and old[1] == modified:
                continue
            try:
                with path.open('rb') as stream:
                    stream.seek(position)
                    while time.monotonic() < deadline and consumed < 64 * 1024 * 1024:
                        line = stream.readline()
                        if not line or not line.endswith(b'\n'):
                            break
                        consumed += len(line)
                        position = stream.tell()
                        try:
                            record = json.loads(line)
                            if record.get('type') != 'assistant':
                                continue
                            message = record.get('message') or {}
                            usage = message.get('usage') or {}
                            identifier = message.get('id') or record.get('uuid')
                            stamp = datetime.datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
                            if not identifier or not usage or stamp < now - 7 * 86400:
                                continue
                            values = [max(0, int(usage.get(key) or 0)) for key in (
                                'input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
                            connection.execute('''INSERT INTO requests VALUES (?,?,?,?,?,?)
                                ON CONFLICT(id) DO UPDATE SET
                                input=MAX(input,excluded.input), output=MAX(output,excluded.output),
                                cached=MAX(cached,excluded.cached), created=MAX(created,excluded.created)''',
                                (identifier, stamp, *values))
                        except (ValueError, TypeError, KeyError, AttributeError):
                            continue
                connection.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)', (str(path), position, modified))
                scanned += 1
            except OSError:
                continue
        connection.execute('DELETE FROM requests WHERE stamp < ?', (now - 8 * 86400,))
        connection.commit()
        completed = 0
        for path, size, modified in files:
            row = connection.execute('SELECT position FROM files WHERE path=?', (str(path),)).fetchone()
            if row and row[0] >= size:
                completed += 1
        def totals(since):
            return connection.execute('SELECT COUNT(*), COALESCE(SUM(input+output+cached+created),0) FROM requests WHERE stamp >= ?', (since,)).fetchone()
        requests, tokens = totals(now - 7 * 86400)
        requests_day, tokens_day = totals(now - 86400)
        return {'tokens_7d': tokens, 'requests_7d': requests, 'tokens_24h': tokens_day,
                'requests_24h': requests_day, 'history_complete': completed == len(files),
                'files_indexed': completed, 'files_total': len(files), 'observed_at': now,
                'basis': 'Processed tokens including cached input; relative workload estimate'}
    finally:
        connection.close()


def binary(name):
    home = Path.home()
    candidates = [shutil.which(name), str(home / '.local/bin' / name),
                  str(home / '.local/share/mise/shims' / name),
                  str(home / '.local/share/mise/installs' / name / 'latest/bin' / name),
                  str(home / '.bun/bin' / name), str(home / '.npm-global/bin' / name),
                  '/opt/homebrew/bin/' + name, '/usr/local/bin/' + name]
    return next((p for p in candidates if p and os.access(p, os.X_OK)), name)


def window(value, percent='usedPercent', reset='resetsAt'):
    if not value:
        return None
    return {'used': value.get(percent), 'reset': value.get(reset),
            'minutes': value.get('windowDurationMins')}


def codex_windows(limits):
    short, weekly = None, None
    for key in ('primary', 'secondary'):
        value = limits.get(key)
        if not value:
            continue
        minutes = value.get('windowDurationMins')
        if minutes is not None and minutes >= 10080:
            weekly = window(value)
        elif minutes is not None:
            short = window(value)
        elif key == 'primary':
            short = window(value)
        else:
            weekly = window(value)
    return short, weekly


def codex():
    proc = subprocess.Popen([binary('codex'), 'app-server'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    buf = b''

    def send(value):
        proc.stdin.write((json.dumps(value) + '\n').encode())
        proc.stdin.flush()

    def request(number, method, params):
        nonlocal buf
        send({'id': number, 'method': method, 'params': params})
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            if b'\n' not in buf:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError('Codex app-server exited')
                buf += data
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                try:
                    response = json.loads(line)
                except ValueError:
                    continue
                if response.get('id') == number:
                    if 'error' in response:
                        raise RuntimeError('Codex could not read account quota')
                    return response['result']
        raise TimeoutError('Codex quota request timed out')

    try:
        request(1, 'initialize', {'clientInfo': {'name': 'tailnet_usage', 'version': '1.0'}})
        send({'method': 'initialized', 'params': {}})
        account = request(2, 'account/read', {'refreshToken': False}).get('account') or {}
        result = request(3, 'account/rateLimits/read', {})
        limits = (result.get('rateLimitsByLimitId') or {}).get('codex') or result.get('rateLimits') or {}
        short, weekly = codex_windows(limits)
        return {'account': account.get('email') or account.get('type') or 'Unknown account',
                'plan': account.get('planType'), 'session': short,
                'weekly': weekly, 'error': None}
    finally:
        selector.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def claude():
    directory = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    config_home = directory if os.environ.get('CLAUDE_CONFIG_DIR') else Path.home()
    config = {}
    for path in (directory / '.config.json', config_home / '.claude.json'):
        try:
            config = json.loads(path.read_text())
            break
        except (OSError, ValueError):
            continue
    output = {'account': config.get('oauthAccount', {}).get('emailAddress') or 'Not signed in',
              'plan': None, 'session': None, 'weekly': None, 'error': None}
    try:
        try:
            credentials = json.loads((directory / '.credentials.json').read_text())
        except FileNotFoundError:
            if os.uname().sysname != 'Darwin':
                raise
            credentials = json.loads(subprocess.check_output(
                ['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                stderr=subprocess.DEVNULL, timeout=10))
        token = credentials.get('claudeAiOauth', {}).get('accessToken')
        output['plan'] = credentials.get('claudeAiOauth', {}).get('subscriptionType')
        if not token:
            raise ValueError('No subscription token')
        request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
            'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'tailnet-usage/1.0'})
        with urllib.request.urlopen(request, timeout=18) as response:
            usage = json.load(response)
        output.update(session=window(usage.get('five_hour'), 'utilization', 'resets_at'),
                      weekly=window(usage.get('seven_day'), 'utilization', 'resets_at'))
    except urllib.error.HTTPError as error:
        output['error'] = {429: 'Claude usage rate limited; retrying next poll',
                           401: 'Claude login expired; sign in on this machine',
                           403: 'Claude usage access denied'}.get(error.code, 'Claude usage HTTP ' + str(error.code))
    except Exception:
        output['error'] = 'Claude quota unavailable; check login and credentials on this machine'
    return output


def collect():
    output = {'collected_at': time.time()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn) for name, fn in [('codex', codex), ('claude', claude), ('workload', workload)]}
        for name, future in futures.items():
            try:
                output[name] = future.result()
            except Exception:
                if name == 'workload':
                    output[name] = {'error': 'Workload history unavailable', 'history_complete': False}
                    continue
                output[name] = {'account': 'Unavailable', 'session': None, 'weekly': None,
                                'error': name.title() + ' unavailable; check installation and login'}
    return output


if __name__ == '__main__':
    print(json.dumps(collect()))
