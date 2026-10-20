import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import switch_worker as worker
from switching import Switches, remote

TARGET = 'worker@example.com'
OLD = 'old@example.com'


def login():
    return {'credentials': {'accessToken': 'synthetic-access', 'expiresAt': (time.time() + 3600) * 1000,
                            'scopes': ['user:inference']}, 'identity': {'emailAddress': TARGET}}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / '.claude.json'
        self.creds = self.home / '.claude/.credentials.json'
        worker.write(self.config, {'oauthAccount': {'emailAddress': OLD}, 'keep': True})
        worker.write(self.creds, {'claudeAiOauth': {'accessToken': 'old', 'refreshToken': 'synthetic-original'}})
        self.request = {'action': 'apply', 'expected_current': OLD, 'target': TARGET,
                        'request_id': 'a' * 32, 'login': login()}

    def run_request(self, request=None, count=lambda: 0):
        return worker.operate(request or self.request, home=self.home, process_counter=count, platform='linux')

    def test_stdin_transport_runs_helper_without_exposing_credentials(self):
        with patch.dict(os.environ, {'HOME': str(self.home)}):
            result = remote({'local': True}, {'action': 'probe'})
        self.assertEqual(result['account'], OLD)
        self.assertIsInstance(result['active_sessions'], int)
        self.assertNotIn('accessToken', json.dumps(result))

    def test_active_session_and_idle_terminal_block_switch(self):
        result = self.run_request(count=lambda: 3)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(worker.read(self.config)['oauthAccount']['emailAddress'], OLD)
        self.assertEqual(worker.read(self.creds)['claudeAiOauth']['accessToken'], 'old')

    def test_apply_backup_permissions_and_no_refresh_token(self):
        self.assertEqual(self.run_request()['status'], 'applied')
        self.assertEqual(worker.read(self.config)['oauthAccount']['emailAddress'], TARGET)
        self.assertTrue(worker.read(self.config)['keep'])
        self.assertNotIn('refreshToken', worker.read(self.creds)['claudeAiOauth'])
        backup = self.home / '.local/state/tailnet-usage/switching/backup-'
        backup = Path(str(backup) + 'a' * 32 + '.json')
        self.assertEqual(worker.read(backup)['credentials']['claudeAiOauth']['refreshToken'], 'synthetic-original')
        self.assertEqual(self.creds.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        probe = self.run_request({'action': 'probe'})
        self.assertEqual(probe['managed_request'], 'a' * 32)

    def test_new_session_between_checks_blocks(self):
        counts = iter([0, 1])
        self.assertEqual(self.run_request(count=lambda: next(counts))['status'], 'waiting')
        self.assertEqual(worker.read(self.creds)['claudeAiOauth']['accessToken'], 'old')

    def test_stale_identity_rejected(self):
        self.request['expected_current'] = 'other@example.com'
        self.assertEqual(self.run_request()['status'], 'conflict')

    def test_shared_refresh_token_rejected(self):
        self.request['login']['credentials']['refreshToken'] = 'never-copy-this'
        with self.assertRaises(ValueError):
            self.run_request()

    def test_sync_rotates_only_same_account_while_work_runs(self):
        self.run_request()
        request = {'action': 'sync', 'target': TARGET, 'login': login()}
        request['login']['credentials']['accessToken'] = 'renewed'
        result = self.run_request(request, count=lambda: 5)
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(worker.read(self.creds)['claudeAiOauth']['accessToken'], 'renewed')
        worker.write(self.config, {'oauthAccount': {'emailAddress': OLD}})
        self.assertEqual(self.run_request(request)['status'], 'conflict')

    def test_manual_native_relogin_stops_sync(self):
        self.run_request()
        worker.write(self.creds, {'claudeAiOauth': {'refreshToken': 'new-native-login'}})
        self.assertEqual(self.run_request({'action': 'sync', 'target': TARGET, 'login': login()})['status'], 'conflict')

    def test_write_failure_rolls_back_both_files(self):
        original = worker.write
        failed = False
        def failing(path, data):
            nonlocal failed
            if path == self.config and not failed:
                failed = True
                raise OSError('synthetic failure')
            original(path, data)
        with patch.object(worker, 'write', failing):
            with self.assertRaises(RuntimeError):
                self.run_request()
        self.assertEqual(worker.read(self.config)['oauthAccount']['emailAddress'], OLD)
        self.assertEqual(worker.read(self.creds)['claudeAiOauth']['accessToken'], 'old')

    def test_mac_keychain_write_is_required(self):
        with patch.object(worker, 'keychain_read', return_value={'claudeAiOauth': {'accessToken': 'old'}}), patch.object(worker, 'keychain_write') as save:
            result = worker.operate(self.request, self.home, lambda: 0, 'darwin')
        self.assertEqual(result['status'], 'applied')
        self.assertNotIn('refreshToken', save.call_args.args[0]['claudeAiOauth'])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jobs.json'
        self.now = time.time()
        self.calls = []
        def run(machine, request):
            self.calls.append(request)
            if request['action'] == 'probe':
                return {'status': 'waiting', 'account': OLD, 'active_sessions': 2}
            return {'status': 'applied', 'expires_at': self.now + 3600}
        self.queue = Switches([{'name': 'worker', 'local': True}], path=self.path, runner=run)
        self.payload = {'machines': [{'name': 'worker', 'reachable': True, 'claude': {'account': OLD}}],
            'claude_accounts': [{'account': TARGET, 'auth_status': 'connected', 'observed_at': self.now,
                'session': {'used': 5}, 'weekly': {'used': 20}}],
            'recommendations': {'assignments': [{'machine': 'worker', 'current': OLD, 'recommended': TARGET, 'action': 'Switch', 'role': 'Worker'}]}}

    def test_queue_survives_restart_and_cancels_without_switch(self):
        self.queue.queue('worker', TARGET, self.payload)
        self.queue.tick(self.payload)
        restored = Switches([{'name': 'worker', 'local': True}], path=self.path)
        self.assertEqual(restored.public()['jobs']['worker']['status'], 'waiting')
        self.queue.cancel('worker')
        self.queue.tick(self.payload)
        self.assertTrue(all(c['action'] == 'probe' for c in self.calls))

    def test_exhausted_or_changed_recommendation_blocks(self):
        self.queue.queue('worker', TARGET, self.payload)
        self.payload['claude_accounts'][0]['weekly']['used'] = 100
        self.queue.tick(self.payload)
        self.assertEqual(self.queue.public()['jobs']['worker']['status'], 'blocked')
        self.assertFalse(any(c['action'] == 'apply' for c in self.calls))

    def test_apply_records_managed_login_and_sanitized_state(self):
        self.queue.runner = lambda m, r: ({'status': 'ready', 'account': OLD} if r['action'] == 'probe' else
                                        {'status': 'applied' if r['action'] == 'apply' else 'synced', 'expires_at': self.now + 3600})
        self.queue.login = lambda target: login()
        self.queue.queue('worker', TARGET, self.payload)
        self.queue.tick(self.payload)
        self.assertEqual(self.queue.public()['jobs']['worker']['status'], 'applied')
        self.assertEqual(self.queue.public()['managed']['worker']['account'], TARGET)
        self.assertNotIn('synthetic-access', self.path.read_text())

    def test_director_reserve_cannot_be_assigned_to_worker(self):
        self.payload['recommendations'].update(director_reserved=True, director_account=TARGET)
        with self.assertRaises(ValueError):
            self.queue.queue('worker', TARGET, self.payload)

    def test_stale_reading_rejected(self):
        self.payload['claude_accounts'][0]['observed_at'] -= 901
        with self.assertRaises(ValueError):
            self.queue.queue('worker', TARGET, self.payload)

    def test_recovery_after_lost_ack(self):
        job = self.queue.queue('worker', TARGET, self.payload)
        self.queue.runner = lambda m, r: {'status': 'ready', 'account': TARGET, 'managed_request': job['id']}
        self.queue.login = lambda target: login()
        self.queue.tick(self.payload)
        self.assertEqual(self.queue.public()['jobs']['worker']['status'], 'applied')

    def test_cancellation_during_probe_prevents_apply(self):
        self.queue.queue('worker', TARGET, self.payload)
        def run(m, r):
            self.queue.cancel('worker')
            return {'status': 'ready', 'account': OLD}
        self.queue.runner = run
        self.queue.login = lambda target: login()
        self.queue.tick(self.payload)
        self.assertEqual(self.queue.public()['jobs']['worker']['status'], 'cancelled')
