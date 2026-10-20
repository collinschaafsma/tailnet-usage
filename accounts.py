"""Dedicated Claude logins. Never copies tokens from machine profiles."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from collector import binary, window
from settings import EMAILS

STATE_DIR = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'tailnet-usage'


def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class AccountStore:
    def __init__(self, root=STATE_DIR, clock=time.time, runner=subprocess.run, opener=urllib.request.urlopen):
        self.root, self.clock, self.runner, self.opener = Path(root), clock, runner, opener

    def directory(self, email):
        if email not in EMAILS:
            raise ValueError('Unknown account')
        return self.root / 'profiles' / email

    @contextmanager
    def locked(self, email, blocking=True):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.directory(email)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(directory, 0o700)
        fd = os.open(directory / '.monitor.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield directory
        finally:
            os.close(fd)

    def environment(self, directory):
        env = os.environ.copy()
        # Only the profile's subscription login may supply authentication.
        for key in list(env):
            if key.startswith(('ANTHROPIC_', 'CLAUDE_CODE_OAUTH', 'CLAUDE_CODE_USE_')) or key == 'CLAUDE_CONFIG_DIR':
                env.pop(key, None)
        env.update(CLAUDE_CONFIG_DIR=str(directory), DISABLE_AUTOUPDATER='1',
                   DISABLE_TELEMETRY='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        return env

    def credentials(self, directory):
        return read_json(directory / '.credentials.json').get('claudeAiOauth', {})

    def identity(self, directory):
        config = read_json(directory / '.config.json') or read_json(directory / '.claude.json')
        return config.get('oauthAccount', {}).get('emailAddress', '').lower()

    def renew(self, directory):
        # This local slash command awaits CLI refresh-and-save; auth status can
        # exit mid-refresh. Do not call a model or implement token rotation here.
        result = self.runner([binary('claude'), '-p', '/usage', '--output-format', 'json',
                              '--no-session-persistence', '--tools', '', '--strict-mcp-config',
                              '--mcp-config', '{"mcpServers":{}}', '--setting-sources', ''],
                             cwd=directory, env=self.environment(directory),
                             capture_output=True, text=True, timeout=90)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {}
        if data.get('num_turns', 0) or data.get('total_cost_usd', 0):
            raise RuntimeError('CLI /usage behavior changed; renewal paused')
        return result.returncode == 0 and not data.get('is_error')

    def usage(self, token):
        request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
            'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'tailnet-usage/1.0'})
        with self.opener(request, timeout=20) as response:
            return json.load(response)

    def public(self, email, saved=None):
        saved = saved or {}
        # Allowlist prevents credentials or raw exception bodies reaching HTTP.
        keys = ('plan', 'session', 'weekly', 'observed_at', 'stale', 'error', 'auth_status',
                'access_expires_at', 'login_expires_at', 'last_renewed_at', 'next_check_at')
        output = {key: saved[key] for key in keys if key in saved}
        output.update(account=email, source='dedicated')
        return output

    def poll(self, email):
        directory = self.directory(email)
        saved = read_json(directory / 'monitor.json')
        try:
            with self.locked(email, blocking=False) as directory:
                saved = read_json(directory / 'monitor.json')
                if saved.get('next_check_at', 0) > self.clock():
                    return self.public(email, saved)
                result = self._poll(email, directory, saved)
                atomic_json(directory / 'monitor.json', result)
                return self.public(email, result)
        except BlockingIOError:
            return self.public(email, dict(saved, stale=True, auth_status='connecting',
                                           error='Account sign-in is in progress'))
        except Exception:
            return self.public(email, dict(saved, stale=True, auth_status='retrying',
                                           error='Account check failed; retrying next poll'))

    def _poll(self, email, directory, saved):
        now = self.clock()
        result = dict(saved, stale=True, next_check_at=now + 300)
        credentials = self.credentials(directory)
        identity = self.identity(directory)
        if not credentials.get('accessToken'):
            return dict(result, auth_status='sign_in_required', error='Connect this account on desktop')
        if identity != email:
            return dict(result, auth_status='wrong_account', error='Saved login does not match this account; sign in again')
        result.update(plan=credentials.get('subscriptionType'),
                      access_expires_at=self.seconds(credentials.get('expiresAt')),
                      login_expires_at=self.seconds(credentials.get('refreshTokenExpiresAt')))
        if result.get('renewal_disabled'):
            return dict(result, auth_status='renewal_paused', error='CLI /usage behavior changed; renewal paused for review')
        try:
            expiry = result.get('access_expires_at')
            if expiry is None or expiry <= now + 600:
                if not credentials.get('refreshToken'):
                    return dict(result, auth_status='sign_in_required', error='No renewable login; sign in again')
                previous_token, previous_expiry = credentials['accessToken'], expiry
                self.renew(directory)
                credentials = self.credentials(directory)
                expiry = self.seconds(credentials.get('expiresAt'))
                if not credentials.get('accessToken') or expiry is None or expiry <= now + 60:
                    return dict(result, auth_status='sign_in_required', error='Login could not be renewed; sign in again')
                result.update(access_expires_at=expiry,
                              login_expires_at=self.seconds(credentials.get('refreshTokenExpiresAt')))
                if credentials['accessToken'] != previous_token or (previous_expiry is not None and expiry > previous_expiry):
                    result['last_renewed_at'] = self.clock()
            try:
                usage = self.usage(credentials['accessToken'])
            except urllib.error.HTTPError as error:
                if error.code != 401:
                    raise
                # One native refresh attempt for early server-side expiration.
                try:
                    self.renew(directory)
                    updated = self.credentials(directory)
                    if updated.get('accessToken') == credentials.get('accessToken'):
                        raise error
                    usage = self.usage(updated['accessToken'])
                    result.update(access_expires_at=self.seconds(updated.get('expiresAt')),
                                  login_expires_at=self.seconds(updated.get('refreshTokenExpiresAt')),
                                  last_renewed_at=self.clock())
                finally:
                    error.close()
            if not any(isinstance(usage.get(key), dict) for key in ('five_hour', 'seven_day')):
                raise ValueError('Unexpected usage format')
            result.update(session=window(usage.get('five_hour'), 'utilization', 'resets_at'),
                          weekly=window(usage.get('seven_day'), 'utilization', 'resets_at'),
                          observed_at=self.clock(), stale=False, error=None, auth_status='connected', failures=0)
        except urllib.error.HTTPError as error:
            result['failures'] = saved.get('failures', 0) + 1
            result['auth_status'] = 'sign_in_required' if error.code in (401, 403) else 'retrying'
            result['error'] = ('Login rejected; sign in again' if error.code in (401, 403)
                               else 'Claude rate limited; backing off' if error.code == 429
                               else 'Claude usage temporarily unavailable')
            delay = min(3600, 300 * 2 ** min(result['failures'] - 1, 4))
            try:
                delay = max(delay, min(86400, int(error.headers.get('Retry-After', 0))))
            except (ValueError, AttributeError):
                pass
            result['next_check_at'] = self.clock() + delay
            error.close()
        except RuntimeError:
            result.update(renewal_disabled=True, auth_status='renewal_paused',
                          error='CLI /usage behavior changed; renewal paused for review')
        except Exception:
            result.update(auth_status='retrying', error='Account check interrupted; retrying next poll')
        return result

    @staticmethod
    def seconds(value):
        return value / 1000 if isinstance(value, (int, float)) else None

    def login(self, email):
        with self.locked(email) as directory:
            print('\nSign in as ' + email + ' for the dashboard. Your normal Claude login stays separate.', flush=True)
            result = subprocess.run([binary('claude'), 'auth', 'login', '--claudeai', '--email', email],
                                    env=self.environment(directory), cwd=directory)
            if result.returncode or self.identity(directory) != email:
                print('Sign-in not completed for ' + email + '. Run this command again.', flush=True)
                return False
            saved = read_json(directory / 'monitor.json')
            saved.update(next_check_at=0, renewal_disabled=False, failures=0)
            atomic_json(directory / 'monitor.json', saved)
            print('Connected ' + email, flush=True)
            return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['login', 'status'])
    parser.add_argument('account', choices=[*EMAILS, 'all'], nargs='?', default='all')
    args = parser.parse_args()
    store = AccountStore()
    for email in EMAILS if args.account == 'all' else [args.account]:
        if args.command == 'login':
            if not store.login(email):
                break
        else:
            print(json.dumps(store.poll(email)))
