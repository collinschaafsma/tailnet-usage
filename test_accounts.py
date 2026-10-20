from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError

from accounts import AccountStore, atomic_json, EMAILS

EMAIL = EMAILS[0]
NOW = 2000000000


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(self.temp.name, clock=lambda: NOW)
        self.directory = self.store.directory(EMAIL)
        self.credentials = {'accessToken': 'test-access', 'refreshToken': 'test-refresh',
                            'expiresAt': (NOW + 7200) * 1000,
                            'refreshTokenExpiresAt': (NOW + 86400) * 1000, 'subscriptionType': 'max'}
        atomic_json(self.directory / '.claude.json', {'oauthAccount': {'emailAddress': EMAIL}})
        self.save_credentials()
        self.calls = []

        def opener(request, **kwargs):
            self.calls.append(request.get_header('Authorization'))
            return io.StringIO(json.dumps({'five_hour': {'utilization': 20, 'resets_at': '2033-05-18T04:00:00Z'},
                                          'seven_day': {'utilization': 90}}))
        self.store.opener = opener

    def save_credentials(self):
        atomic_json(self.directory / '.credentials.json', {'claudeAiOauth': self.credentials})

    def test_missing_login_does_not_borrow_machine_credentials(self):
        (self.directory / '.credentials.json').unlink()
        result = self.store.poll(EMAIL)
        self.assertEqual(result['auth_status'], 'sign_in_required')
        self.assertFalse(self.calls)

    def test_identity_mismatch_blocks_network(self):
        atomic_json(self.directory / '.claude.json', {'oauthAccount': {'emailAddress': 'wrong@example.com'}})
        self.assertEqual(self.store.poll(EMAIL)['auth_status'], 'wrong_account')
        self.assertFalse(self.calls)

    def test_poll_throttles_and_never_exposes_credentials(self):
        result = self.store.poll(EMAIL)
        self.assertEqual(result['weekly']['used'], 90)
        self.assertEqual(result['auth_status'], 'connected')
        self.store.poll(EMAIL)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('test-access', json.dumps(result))
        self.assertNotIn('test-refresh', (self.directory / 'monitor.json').read_text())
        self.assertEqual(os.stat(self.directory / 'monitor.json').st_mode & 0o777, 0o600)

    def test_expiring_token_renewed_and_saved_token_reread(self):
        self.credentials['expiresAt'] = (NOW + 120) * 1000
        self.save_credentials()

        def runner(command, **kwargs):
            self.assertIn('/usage', command)
            self.assertIn('--no-session-persistence', command)
            self.assertEqual(kwargs['env']['CLAUDE_CONFIG_DIR'], str(self.directory))
            self.credentials.update(accessToken='renewed-access', refreshToken='rotated-refresh',
                                    expiresAt=(NOW + 28800) * 1000)
            self.save_credentials()
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(
                {'type': 'result', 'num_turns': 0, 'total_cost_usd': 0, 'is_error': False}))

        self.store.runner = runner
        result = self.store.poll(EMAIL)
        self.assertEqual(self.calls, ['Bearer renewed-access'])
        self.assertEqual(result['last_renewed_at'], NOW)
        self.assertEqual(result['access_expires_at'], NOW + 28800)
        self.assertEqual(self.store.credentials(self.directory)['refreshToken'], 'rotated-refresh')

    def test_rate_limit_keeps_reading_across_restart_and_backs_off(self):
        first = self.store.poll(EMAIL)
        self.store.clock = lambda: NOW + 301

        def unavailable(*args, **kwargs):
            raise HTTPError('https://example.com', 429, 'secret-body', {'Retry-After': '900'}, None)

        self.store.opener = unavailable
        result = self.store.poll(EMAIL)
        self.assertTrue(result['stale'])
        self.assertEqual(result['weekly'], first['weekly'])
        self.assertEqual(result['next_check_at'], NOW + 1201)
        self.assertNotIn('secret-body', json.dumps(result))
        restarted = AccountStore(self.temp.name, clock=lambda: NOW + 302, opener=unavailable)
        self.assertEqual(restarted.poll(EMAIL)['observed_at'], NOW)

    def test_native_refresh_failure_retains_usage(self):
        self.store.poll(EMAIL)
        self.store.clock = lambda: NOW + 301
        self.credentials['expiresAt'] = (NOW + 302) * 1000
        self.save_credentials()
        self.store.runner = lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout='{}')
        result = self.store.poll(EMAIL)
        self.assertEqual(result['auth_status'], 'sign_in_required')
        self.assertEqual(result['weekly']['used'], 90)
        self.assertTrue(result['stale'])

    def test_native_noop_is_not_reported_as_successful_rotation(self):
        self.credentials['expiresAt'] = (NOW + 480) * 1000
        self.save_credentials()
        self.store.runner = lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout='{"num_turns":0,"total_cost_usd":0}')
        result = self.store.poll(EMAIL)
        self.assertEqual(result['auth_status'], 'connected')
        self.assertNotIn('last_renewed_at', result)

    def test_early_401_allows_one_refresh_and_retries_new_token(self):
        def opener(request, **kwargs):
            token = request.get_header('Authorization')
            self.calls.append(token)
            if token == 'Bearer test-access':
                raise HTTPError('https://example.com', 401, 'Unauthorized', {}, io.BytesIO())
            return io.StringIO('{"seven_day":{"utilization":45}}')

        def runner(command, **kwargs):
            self.credentials.update(accessToken='new-access', expiresAt=(NOW + 28800) * 1000)
            self.save_credentials()
            return subprocess.CompletedProcess(command, 0, stdout='{"num_turns":0,"total_cost_usd":0}')

        self.store.opener, self.store.runner = opener, runner
        result = self.store.poll(EMAIL)
        self.assertEqual(result['auth_status'], 'connected')
        self.assertEqual(self.calls, ['Bearer test-access', 'Bearer new-access'])
        self.assertEqual(result['weekly']['used'], 45)

    def test_cli_behavior_change_disables_future_renewals(self):
        self.credentials['expiresAt'] = (NOW + 120) * 1000
        self.save_credentials()
        calls = []

        def runner(*args, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess(args, 0, stdout='{"num_turns": 1, "total_cost_usd": 0.001}')

        self.store.runner = runner
        self.assertEqual(self.store.poll(EMAIL)['auth_status'], 'renewal_paused')
        self.store.clock = lambda: NOW + 301
        self.store.poll(EMAIL)
        self.assertEqual(len(calls), 1)

    def test_login_lock_prevents_polling_and_renewal(self):
        with self.store.locked(EMAIL):
            self.assertEqual(self.store.poll(EMAIL)['auth_status'], 'connecting')
            self.assertFalse(self.calls)

    def test_profile_environment_ignores_inherited_provider_tokens(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'inherited-secret',
                                    'CLAUDE_CODE_OAUTH_TOKEN': 'other-secret',
                                    'CLAUDE_CODE_USE_BEDROCK': '1'}):
            env = self.store.environment(self.directory)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', env)


if __name__ == '__main__':
    unittest.main()
