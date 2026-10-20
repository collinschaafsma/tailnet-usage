import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from accounts import atomic_json, read_json
from notifications import AlertEngine, SendError, candidates, send_twilio

NOW = 2000000000
EMAILS = ['director@example.com', 'worker1@example.com', 'worker2@example.com', 'worker3@example.com']


def snapshot(now=NOW, low=False, target='director@example.com'):
    accounts = [dict(account=email, auth_status='connected', observed_at=now,
                     session={'used': 0, 'reset': now + 10000},
                     weekly={'used': (90 if low else 60) if email == 'worker3@example.com' else 0,
                             'reset': now + 86400}) for email in EMAILS]
    return {'updated_at': now, 'claude_accounts': accounts,
            'recommendations': {'minimum_session_remaining': 30, 'minimum_weekly_remaining': 20,
                'assignments': [{'machine': 'director', 'role': 'Director', 'action': 'Switch first',
                                 'current': 'worker3@example.com', 'recommended': target}]}}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config_path, self.state_path = root / 'config.json', root / 'events.json'
        self.config = {'account_sid': 'AC' + 'b' * 32, 'auth_token': 'a' * 32,
                       'from_number': '+13035550123', 'to_number': '+13035550456',
                       'enabled': True, 'daily_limit': 6, 'cooldown_seconds': 3600}
        atomic_json(self.config_path, self.config)
        self.now = NOW
        self.sent = []

        def sender(config, body):
            self.sent.append(body)
            return {'sid': 'SM' + 'c' * 32, 'status': 'queued'}

        self.sender = sender
        self.engine = self.new_engine()

    def new_engine(self):
        return AlertEngine(self.config_path, self.state_path, clock=lambda: self.now, sender=self.sender)

    def test_unconfigured_and_disabled_never_send(self):
        self.config_path.unlink()
        self.engine.tick(snapshot(low=True))
        self.assertFalse(self.sent)
        self.config['enabled'] = False
        atomic_json(self.config_path, self.config)
        self.engine.tick(snapshot(low=True))
        self.assertFalse(self.sent)

    def test_incomplete_startup_does_not_generate_actions(self):
        self.engine.tick({'claude_accounts': []})
        self.assertFalse(self.sent)

    def test_director_risk_immediate_deduplicated_across_restarts(self):
        self.engine.tick(snapshot(low=True))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('director director', self.sent[0])
        self.assertIn('director@example.com', self.sent[0])
        self.now += 600
        self.new_engine().tick(snapshot(now=self.now, low=True))
        self.assertEqual(len(self.sent), 1)

    def test_nonurgent_assignment_requires_two_minutes(self):
        self.engine.tick(snapshot())
        self.assertFalse(self.sent)
        self.now += 121
        self.engine.tick(snapshot(now=self.now))
        self.assertEqual(len(self.sent), 1)

    def test_setup_confirmation_does_not_block_first_real_alert_or_consume_cap(self):
        self.config['daily_limit'] = 1
        atomic_json(self.config_path, self.config)
        self.engine.tick({}, test=True)
        self.assertEqual(len(self.sent), 1)
        self.now += 10
        self.engine.tick(snapshot(now=self.now))
        self.now += 121
        self.engine.tick(snapshot(now=self.now))
        self.assertEqual(len(self.sent), 2)
        self.assertIn('switch Claude', self.sent[-1])
        self.now += 60
        self.engine.tick(snapshot(now=self.now, low=True, target='worker2@example.com'))
        self.assertEqual(len(self.sent), 2)  # The real alert still consumes the cap.

    def test_legacy_setup_confirmation_does_not_start_cooldown(self):
        atomic_json(self.state_path, {'history': [{'at': NOW - 10, 'status': 'submitted',
            'body': 'Tailnet: Tailnet usage: Telegram alerts are connected. Account-switch alerts are ready.'}]})
        self.engine.tick(snapshot())
        self.now += 121
        self.engine.tick(snapshot(now=self.now))
        self.assertEqual(len(self.sent), 1)

    def test_exhausted_account_immediately_reports_affected_switches(self):
        data = snapshot()
        data['claude_accounts'][1]['weekly']['used'] = 100
        data['recommendations']['assignments'].append({
            'machine': 'worker', 'role': 'Worker', 'action': 'Switch',
            'current': 'worker1@example.com', 'recommended': 'worker2@example.com'})
        self.engine.tick(data)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('worker1@example.com is exhausted', self.sent[0])
        self.assertIn('weekly limit reached', self.sent[0])
        self.assertIn('worker -> worker2@example.com', self.sent[0])
        self.assertIn('Resets', self.sent[0])
        self.now += 600
        data = snapshot(now=self.now)
        data['claude_accounts'][1]['weekly']['used'] = 100
        self.new_engine().tick(data)
        self.assertEqual(len(self.sent), 1)

    def test_exhaustion_bypasses_operational_cooldown(self):
        self.engine.tick(snapshot(low=True))
        self.now += 60
        data = snapshot(now=self.now, low=True)
        data['claude_accounts'][1]['weekly']['used'] = 100
        self.engine.tick(data)
        self.assertEqual(len(self.sent), 2)
        self.assertIn('is exhausted', self.sent[-1])

    def test_stale_usage_or_exhausted_target_never_suggests_switch(self):
        data = snapshot(low=True)
        data['claude_accounts'][-1]['stale'] = True
        self.engine.tick(data)
        self.assertFalse(self.sent)
        data = snapshot(low=True)
        data['claude_accounts'][0]['weekly']['used'] = 100
        events = candidates(data, {}, self.now)
        self.assertNotIn('Switch to', events[0]['body'])

    def test_critical_bypasses_cooldown_but_not_daily_cap(self):
        self.config['daily_limit'] = 1
        atomic_json(self.config_path, self.config)
        self.engine.tick(snapshot(low=True))
        self.now += 61
        self.engine.tick(snapshot(now=self.now, low=True, target='worker2@example.com'))
        self.assertEqual(len(self.sent), 1)

    def test_reset_alert_requires_confirmed_fresh_recovery(self):
        data = snapshot()
        data['recommendations']['assignments'] = []
        data['claude_accounts'][1]['weekly']['used'] = 100
        self.engine.tick(data)
        self.assertIn('is exhausted', self.sent[0])
        self.sent.clear()
        self.now += 3601
        data = snapshot(now=self.now)
        data['recommendations']['assignments'] = []
        data['claude_accounts'][1]['stale'] = True
        self.engine.tick(data)
        self.assertFalse(self.sent)
        data['claude_accounts'][1]['stale'] = False
        self.engine.tick(data)
        self.now += 121
        data = snapshot(now=self.now)
        data['recommendations']['assignments'] = []
        self.engine.tick(data)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('available again', self.sent[0])

    def test_uncertain_submission_not_blindly_retried(self):
        def uncertain(config, body):
            self.sent.append(body)
            raise SendError('Submission uncertain', uncertain=True)
        self.sender = uncertain
        self.engine = self.new_engine()
        self.engine.tick(snapshot(low=True))
        self.now += 7200
        self.new_engine().tick(snapshot(now=self.now, low=True))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.engine.public()['history'][-1]['status'], 'uncertain')

    def test_outbox_persisted_before_network_request(self):
        def inspect(config, body):
            state = read_json(self.state_path)
            self.assertEqual(state['history'][-1]['status'], 'sending')
            return {'sid': 'SM' + 'c' * 32, 'status': 'queued'}
        self.sender = inspect
        self.new_engine().tick(snapshot(low=True))

    def test_429_retries_after_backoff_with_same_condition(self):
        def retry(config, body):
            self.sent.append(body)
            if len(self.sent) == 1:
                raise SendError('Rate limited', retryable=True)
            return {'sid': 'SM' + 'c' * 32, 'status': 'queued'}
        self.sender = retry
        self.engine = self.new_engine()
        self.engine.tick(snapshot(low=True))
        self.now += 600
        self.engine.tick(snapshot(now=self.now, low=True))
        self.assertEqual(len(self.sent), 1)
        self.now += 3001
        self.engine.tick(snapshot(now=self.now, low=True))
        self.assertEqual(len(self.sent), 2)

    def test_public_status_redacts_credentials_and_full_phone(self):
        self.engine.tick(snapshot(low=True))
        output = json.dumps(self.engine.public())
        self.assertNotIn(self.config['auth_token'], output)
        self.assertNotIn(self.config['account_sid'], output)
        self.assertNotIn(self.config['to_number'], output)
        self.assertIn('0456', output)
        self.assertLessEqual(len(self.sent[0]), 300)
        self.assertTrue(self.sent[0].isascii())

    def test_twilio_request_uses_expected_destination_and_redacts_error_body(self):
        def reject(request, **kwargs):
            self.assertEqual(request.method, 'POST')
            self.assertTrue(request.full_url.startswith('https://api.twilio.com/'))
            self.assertIn('To=%2B13035550456', request.data.decode())
            raise HTTPError(request.full_url, 400, 'sensitive-provider-error', {}, io.BytesIO(b'secret'))
        with self.assertRaises(SendError) as error:
            send_twilio(self.config, 'Test', opener=reject)
        self.assertNotIn('sensitive', str(error.exception))
        self.assertFalse(error.exception.uncertain)


if __name__ == '__main__':
    unittest.main()
