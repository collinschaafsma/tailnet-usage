import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector import workload
from recommendations import Advisor, quota

NOW = 2000000000


def account(email, used=0, days=5, short=0, **extra):
    return dict(account=email, observed_at=NOW, auth_status='connected',
                session={'used': short, 'reset': NOW + 3600},
                weekly={'used': used, 'reset': NOW + days * 86400}, **extra)


def machine(name, email, tokens=700, **extra):
    return dict(name=name, reachable=True, claude={'account': email},
                workload={'tokens_7d': tokens, 'tokens_24h': tokens / 7,
                          'requests_7d': 7, 'history_complete': True}, **extra)


class RecommendationTests(unittest.TestCase):
    def plan(self, machines, accounts, **config):
        return Advisor.plan(machines, accounts, config, NOW)

    def test_reserves_strongest_account_for_director_and_moves_exhausted_workers(self):
        machines = [machine('director', 'busy'), machine('dashboard', 'dead'), machine('worker1', 'dead')]
        result = self.plan(machines, [account('reserve'), account('busy', 70), account('dead', 100)])
        self.assertEqual(result['director_account'], 'reserve')
        rows = {a['machine']: a for a in result['assignments']}
        self.assertEqual(rows['director']['action'], 'Switch first')
        self.assertEqual(rows['dashboard']['recommended'], 'busy')
        self.assertEqual(rows['worker1']['recommended'], 'busy')

    def test_shared_director_account_is_never_assigned_to_workers(self):
        result = self.plan([machine('director', 'reserve'), machine('dashboard', 'reserve')],
                           [account('reserve'), account('worker', 20)])
        self.assertEqual(result['assignments'][1]['recommended'], 'worker')

    def test_healthy_director_stays_when_improvement_is_small(self):
        result = self.plan([machine('director', 'current')], [account('current', 10), account('alternative', 5)])
        self.assertEqual(result['director_account'], 'current')

    def test_heavier_machine_gets_larger_remaining_budget(self):
        result = self.plan([machine('director', 'reserve'), machine('heavy', 'small', 70000),
                            machine('light', 'large', 700)],
                           [account('reserve'), account('large', 20), account('small', 70)])
        rows = {a['machine']: a for a in result['assignments']}
        self.assertEqual(rows['heavy']['recommended'], 'large')
        self.assertEqual(rows['light']['recommended'], 'small')

    def test_no_workers_spend_only_director_account(self):
        result = self.plan([machine('director', 'only'), machine('dashboard', 'only')], [account('only')])
        self.assertIsNone(result['assignments'][1]['recommended'])
        self.assertEqual(result['assignments'][1]['action'], 'Pause Claude')

    def test_stale_account_is_excluded(self):
        result = self.plan([machine('director', 'stale')], [account('stale', stale=True), account('healthy', 30)])
        self.assertEqual(result['director_account'], 'healthy')

    def test_stale_timestamp_and_reset_passed_need_new_reading(self):
        old = account('old')
        old['observed_at'] = NOW - 901
        passed = account('passed')
        passed['weekly']['reset'] = NOW - 1
        self.assertIsNone(quota(old, NOW, 900))
        self.assertIsNone(quota(passed, NOW, 900))

    def test_both_exhausted_windows_must_reset(self):
        exhausted = account('empty', used=100, short=100)
        exhausted['session']['reset'] = NOW + 60
        exhausted['weekly']['reset'] = NOW + 3600
        result = self.plan([machine('director', 'empty')], [exhausted])
        self.assertIsNone(result['director_account'])
        self.assertEqual(result['excluded_accounts'][0]['recheck_at'], NOW + 3600)

    def test_director_margin_and_missing_data_warn_instead_of_guaranteeing_safety(self):
        result = self.plan([machine('director', 'low')], [account('low', used=90, short=80)])
        self.assertTrue(any('safety margins' in w for w in result['warnings']))
        missing = account('unknown')
        missing['session'] = None
        self.assertIsNone(self.plan([machine('director', 'unknown')], [missing])['director_account'])

    def test_reset_time_changes_worker_pacing_capacity(self):
        soon = quota(account('soon', 60, days=1), NOW, 900)
        later = quota(account('later', 60, days=6), NOW, 900)
        self.assertGreater(soon['capacity'], later['capacity'])

    def test_history_does_not_infer_burn_across_weekly_resets(self):
        with tempfile.TemporaryDirectory() as root:
            advisor = Advisor(Path(root) / 'history.json')
            advisor.history = {'account': [
                {'at': NOW + i * 1200, 'used': i * 2, 'reset': NOW + (i + 1) * 86400,
                 'members': {'director': 1}} for i in range(5)]}
            self.assertEqual(advisor.burn_rates(), {})

    def test_history_persists_and_learns_stable_assignment_burn(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.json'
            advisor = Advisor(path)
            advisor.history = {'account': [
                {'at': NOW + i * 1200, 'used': i * 2, 'reset': NOW + 86400,
                 'members': {'director': 1}} for i in range(5)]}
            self.assertAlmostEqual(advisor.burn_rates()['director'], 144)
            now = NOW + 6000
            reading = account('account', used=10)
            reading['observed_at'] = now
            advisor.remember([machine('director', 'account')], [reading], now)
            restored = Advisor(path)
            self.assertEqual(len(restored.history['account']), 6)


class WorkloadTests(unittest.TestCase):
    def test_deduplicates_streaming_messages_and_keeps_partial_lines_for_next_scan(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'claude'
            logs = directory / 'projects' / 'fixture'
            logs.mkdir(parents=True)
            record = {'type': 'assistant', 'timestamp': '2033-05-18T03:33:20Z',
                      'message': {'id': 'same-message', 'usage': {'input_tokens': 10, 'output_tokens': 20,
                                                               'cache_read_input_tokens': 30}}}
            path = logs / 'session.jsonl'
            path.write_text(json.dumps(record) + '\n' + json.dumps(record) + '\n' + '{"partial":')
            os.utime(path, (NOW, NOW))
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(directory), 'XDG_STATE_HOME': root}), patch('collector.time.time', return_value=NOW):
                first = workload()
                self.assertEqual(first['requests_7d'], 1)
                self.assertEqual(first['tokens_7d'], 60)
                self.assertFalse(first['history_complete'])
                with path.open('a') as stream:
                    stream.write('true}\n')
                second = workload()
                self.assertTrue(second['history_complete'])
                self.assertEqual(second['tokens_7d'], 60)


if __name__ == '__main__':
    unittest.main()
