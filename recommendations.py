"""Deterministic assignment advice from live quotas and measured local workload."""
import datetime
import itertools
import math
from pathlib import Path
import statistics
import threading
import time

from accounts import STATE_DIR, atomic_json, read_json


def timestamp(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quota(account, now, freshness):
    if account.get('stale') or account.get('error') or account.get('auth_status') != 'connected':
        return None
    observed = account.get('observed_at')
    if not number(observed) or now - observed > freshness:
        return None
    windows = [account.get(key) or {} for key in ('session', 'weekly')]
    if not all(number(w.get('used')) and 0 <= w['used'] <= 100 for w in windows):
        return None
    resets = [timestamp(w.get('reset')) for w in windows]
    if any(reset is not None and reset <= now for reset in resets):
        return None
    if any(w['used'] >= 100 for w in windows):
        return None
    short, weekly = (100 - w['used'] for w in windows)
    days = max(0.25, (resets[1] - now) / 86400) if resets[1] else 7
    # Relative pacing budget; this is not a conversion from tokens to quota.
    capacity = max(0.01, weekly / days * short / 100)
    return {'account': account['account'], 'session_remaining': short,
            'weekly_remaining': weekly, 'weekly_reset': resets[1],
            'session_reset': resets[0], 'daily_budget': weekly / days, 'capacity': capacity}


class Advisor:
    def __init__(self, path=STATE_DIR / 'recommendation-history.json', clock=time.time):
        self.path, self.clock = Path(path), clock
        self.history = read_json(self.path).get('samples', {})
        self.lock = threading.Lock()

    def remember(self, machines, accounts, now):
        changed = False
        for account in accounts:
            email = account['account']
            observed = account.get('observed_at')
            weekly = account.get('weekly') or {}
            reset = timestamp(weekly.get('reset'))
            if account.get('stale') or account.get('error') or not number(observed) or not number(weekly.get('used')) or reset is None:
                continue
            samples = self.history.setdefault(email, [])
            if samples and samples[-1]['at'] >= observed:
                continue
            members = {}
            for machine in machines:
                work = machine.get('workload') or {}
                if (machine.get('claude') or {}).get('account') == email and machine.get('reachable') and work.get('history_complete') and not work.get('error'):
                    weight = max(work.get('tokens_24h', 0), work.get('tokens_7d', 0) / 7)
                    members[machine['name']] = max(1, weight)
            samples.append({'at': observed, 'used': weekly['used'], 'reset': reset, 'members': members})
            self.history[email] = [s for s in samples if s['at'] >= now - 7 * 86400][-2100:]
            changed = True
        if changed:
            atomic_json(self.path, {'samples': self.history})

    def burn_rates(self):
        # Account-wide percentage changes are apportioned by local workload.
        # Only stable assignments within the same quota window are comparable.
        totals = {}
        for samples in self.history.values():
            for a, b in zip(samples, samples[1:]):
                duration = b['at'] - a['at']
                if not (60 <= duration <= 3600) or abs(a['reset'] - b['reset']) > 120 or a['members'].keys() != b['members'].keys():
                    continue
                delta = b['used'] - a['used']
                if not 0 <= delta <= 100 or not b['members']:
                    continue
                weight = sum(b['members'].values())
                for machine, value in b['members'].items():
                    item = totals.setdefault(machine, {'used': 0, 'seconds': 0, 'samples': 0})
                    item['used'] += delta * value / weight
                    item['seconds'] += duration
                    item['samples'] += 1
        return {machine: value['used'] / value['seconds'] * 86400
                for machine, value in totals.items()
                if value['seconds'] >= 3600 and value['samples'] >= 3 and value['used'] > 0}

    def evaluate(self, machines, accounts, config):
        with self.lock:
            now = self.clock()
            try:
                self.remember(machines, accounts, now)
                history_error = False
            except OSError:
                history_error = True
            return self.plan(machines, accounts, config, now, self.burn_rates(), history_error)

    @staticmethod
    def plan(machines, accounts, config, now, rates=None, history_error=False):
        rates = rates or {}
        director_name = config.get('director_machine', 'director')
        reserve = config.get('reserve_director_account', True)
        min_short = config.get('director_min_session_remaining', 30)
        min_week = config.get('director_min_weekly_remaining', 20)
        freshness = config.get('freshness_seconds', 900)
        capacities = {a['account']: q for a in accounts if (q := quota(a, now, freshness))}
        by_name = {m['name']: m for m in machines}
        director = by_name.get(director_name)
        warnings = []
        if history_error:
            warnings.append('Usage trend history could not be saved; workload-based advice is still available.')
        incomplete = [m['name'] for m in machines if not (m.get('workload') or {}).get('history_complete')]
        if incomplete:
            warnings.append('Learning workload history on ' + ', '.join(incomplete) + '. Initial estimates may change.')
        usable = list(capacities)
        safe = [q for q in capacities.values() if q['session_remaining'] >= min_short and q['weekly_remaining'] >= min_week]
        director_rate = rates.get(director_name)
        def director_score(q):
            score = min(q['session_remaining'] / min_short, q['weekly_remaining'] / min_week)
            if director_rate:
                score = min(score, q['daily_budget'] / (director_rate * 1.3))
            return score + min(q['daily_budget'], 100) / 1000
        choices = safe or list(capacities.values())
        selected = max(choices, key=lambda q: (director_score(q), q['account'] == config.get('preferred_director_account'))) if choices else None
        current_director = (director.get('claude') or {}).get('account') if director else None
        current_capacity = capacities.get(current_director)
        # Prefer staying put unless a materially safer option exists.
        if selected and current_capacity in safe and director_score(current_capacity) >= director_score(selected) * .85:
            selected = current_capacity
        if selected and not safe:
            warnings.append('No account meets the director safety margins. Slow or pause workers and watch the director closely.')
        if director and not director.get('reachable'):
            warnings.append(director_name + ' is unreachable; its current director login needs verification before any switch.')
        if not selected:
            warnings.append('No account has fresh usable session and weekly quota. Keep the director paused until a fresh reading confirms capacity.')
        director_email = selected['account'] if selected else None
        if selected and current_director != director_email:
            warnings.append('Priority: move ' + director_name + '’s director from ' + (current_director or 'unknown') + ' to ' + director_email + '.')
        if selected and director_rate and director_rate * 1.3 > selected['daily_budget']:
            warnings.append('Estimated director burn exceeds the available pace to weekly reset, even on the best account. Reduce work or arrange a fallback.')
        pool = [email for email in usable if not reserve or email != director_email]
        workers = [m for m in machines if m['name'] != director_name and m.get('reachable')]
        measured = []
        for m in machines:
            w = m.get('workload') or {}
            if not w.get('error'):
                value = max(w.get('tokens_24h', 0), w.get('tokens_7d', 0) / 7)
                if value > 0:
                    measured.append(value)
        baseline = statistics.median(measured) if measured else 1
        weights = {}
        for m in machines:
            w = m.get('workload') or {}
            value = max(w.get('tokens_24h', 0), w.get('tokens_7d', 0) / 7)
            weights[m['name']] = max(.1, value / baseline) if value else 1
        assignments = {}
        if workers and pool:
            best_cost = math.inf
            # Five-machine fleet: at most four workers and four accounts.
            for assignment in itertools.product(pool, repeat=len(workers)):
                loads = {email: 0 for email in pool}
                changes = 0
                for machine, email in zip(workers, assignment):
                    loads[email] += weights[machine['name']]
                    changes += (machine.get('claude') or {}).get('account') != email
                if not reserve and director_email in loads:
                    loads[director_email] += weights.get(director_name, 1) * 2
                cost = sum((loads[email] / capacities[email]['capacity']) ** 2 for email in pool)
                cost += changes * .0005
                if cost < best_cost:
                    best_cost = cost
                    assignments = {m['name']: email for m, email in zip(workers, assignment)}
        if workers and not pool:
            warnings.append('No fresh worker account is available without using the director reserve. Pause Claude workers until another account recovers.')
        planned = dict(assignments)
        if director_email:
            planned[director_name] = director_email
        for email in set(planned.values()):
            members = [name for name, target in planned.items() if target == email]
            q = capacities[email]
            if all(name in rates for name in members):
                demand = sum(rates[name] for name in members)
                if demand * 1.3 > q['daily_budget']:
                    warnings.append(email + ' may run out before weekly reset at the observed pace; reduce work or plan a later rotation.')
            elif email != director_email and (q['session_remaining'] < 10 or q['weekly_remaining'] < 5):
                warnings.append(email + ' has little worker headroom; defer heavy Claude tasks until reset.')
        rows = []
        for machine in machines:
            name = machine['name']
            current = (machine.get('claude') or {}).get('account')
            target = director_email if name == director_name else assignments.get(name)
            q = capacities.get(target)
            work = machine.get('workload') or {}
            rate = rates.get(name)
            if not machine.get('reachable'):
                action, reason = 'Check machine', 'Machine is unavailable; confirm its login before changing assignments.'
                target = None
            elif not target:
                action, reason = 'Pause Claude', 'No safe account allocation is available with current readings.'
            elif name == director_name:
                action = 'Keep' if current == target else 'Switch first'
                reason = ('Dedicated to the director. ' if reserve else 'Director gets first claim on quota. ')
                reason += f"{q['session_remaining']:.0f}% session and {q['weekly_remaining']:.0f}% weekly quota remain."
            else:
                action = 'Keep' if current == target else 'Switch'
                reason = f"Balances measured workload with {q['weekly_remaining']:.0f}% weekly headroom"
                reason += ' while keeping the director account free.' if reserve else '.'
            rows.append({'machine': name, 'role': 'Director' if name == director_name else 'Worker',
                         'current': current, 'recommended': target, 'action': action, 'reason': reason,
                         'workload': work, 'relative_load': round(weights[name], 2),
                         'estimated_weekly_points_per_day': round(rate, 1) if rate else None,
                         'weekly_reset': q['weekly_reset'] if q else None})
        excluded = []
        for account in accounts:
            if account['account'] in capacities:
                continue
            exhausted = [w for key in ('session', 'weekly') if (w := account.get(key) or {}) and number(w.get('used')) and w['used'] >= 100]
            future_resets = [r for w in exhausted if (r := timestamp(w.get('reset'))) and r > now]
            reason = 'Limit reached' if exhausted else 'Fresh usage or working login needed'
            # Every exhausted window must reset before reuse is possible.
            reset = max(future_resets) if future_resets and len(future_resets) == len(exhausted) else None
            excluded.append({'account': account['account'], 'reason': reason, 'recheck_at': reset})
        return {'generated_at': now, 'director_account': director_email, 'director_reserved': reserve,
                'minimum_session_remaining': min_short, 'minimum_weekly_remaining': min_week,
                'assignments': rows, 'warnings': warnings, 'excluded_accounts': excluded,
                'learning_burn_rates': not bool(rates),
                'method': 'Uses the higher of 24-hour token volume or the seven-day daily average, remaining quota, and time to reset. Token counts include cached input and are a relative workload proxy, not exact quota consumption. Trends need at least one hour of stable observations. Recommendations do not change logins.'}
