"""Durable opt-in switches. Only access tokens travel over SSH stdin."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from accounts import AccountStore, STATE_DIR, atomic_json, read_json
from recommendations import quota

ROOT = Path(__file__).resolve().parent
PENDING = ('queued', 'waiting', 'blocked', 'applying')


def remote(machine, request):
    source = (ROOT / 'switch_worker.py').read_text()
    # JSON is data in stdin, never interpolated into the remote shell command.
    bootstrap = 'import json,sys; d=json.load(sys.stdin); sys.stdin=__import__("io").StringIO(json.dumps(d["request"])); exec(compile(d["source"],"switch_worker.py","exec"))'
    import shlex
    command = [sys.executable, '-c', bootstrap] if machine.get('local') else [
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'StrictHostKeyChecking=yes',
        machine['ssh'], 'python3 -c ' + shlex.quote(bootstrap)]
    result = subprocess.run(command, input=json.dumps({'source': source, 'request': request}),
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Machine unavailable')
    return json.loads(result.stdout)


class Switches:
    def __init__(self, machines, path=STATE_DIR / 'switch-jobs.json', store=None, runner=remote, clock=time.time):
        self.machines = {m['name']: m for m in machines}
        self.path, self.store, self.runner, self.clock = Path(path), store or AccountStore(), runner, clock
        self.data = read_json(self.path) or {'jobs': {}, 'managed': {}}
        self.lock = threading.RLock()
        self.wake = threading.Event()

    def save(self):
        atomic_json(self.path, self.data)

    def public(self):
        with self.lock:
            return copy.deepcopy(self.data)

    @staticmethod
    def allowed(payload, machine, target, now):
        advice = payload['recommendations']
        row = next((r for r in advice['assignments'] if r['machine'] == machine), {})
        reading = next((r for r in payload['machines'] if r['name'] == machine), {})
        account = next((a for a in payload['claude_accounts'] if a['account'] == target), {})
        available = quota(account, now, 900)
        if row.get('recommended') != target or row.get('current') == target or not row.get('action', '').startswith('Switch'):
            raise ValueError('Recommendation changed. Reload before switching.')
        if not reading.get('reachable') or not available:
            raise ValueError('Fresh machine and target account readings are required.')
        if advice.get('director_reserved') and target == advice.get('director_account'):
            if row.get('role') != 'Director':
                # Role strings vary; use director assignment identity as fallback below.
                if 'director' not in row.get('role', '').lower():
                    raise ValueError('This account is reserved for the director.')
            others = [m for m in payload['machines'] if m['name'] != machine and
                      (m.get('claude') or {}).get('account') == target]
            if others:
                raise ValueError('Move workers off the director reserve first.')
        return row

    def queue(self, machine, target, payload):
        if machine not in self.machines:
            raise ValueError('Unknown machine.')
        with self.lock:
            if self.data['jobs'].get(machine, {}).get('status') in PENDING:
                raise ValueError('A switch is already queued for this machine.')
            row = self.allowed(payload, machine, target, self.clock())
            job = {'id': uuid.uuid4().hex, 'machine': machine, 'target': target,
                   'expected_current': row.get('current'), 'created_at': self.clock(),
                   'status': 'queued', 'message': 'Queued; checking for open Claude sessions.'}
            self.data['jobs'][machine] = job
            self.save()
            self.wake.set()
            return copy.deepcopy(job)

    def cancel(self, machine):
        with self.lock:
            job = self.data['jobs'].get(machine, {})
            if job.get('status') == 'applying':
                raise ValueError('Login write has started; wait for its result.')
            if job.get('status') not in PENDING:
                raise ValueError('No pending switch for this machine.')
            job.update(status='cancelled', message='Switch cancelled.')
            self.save()

    def login(self, target):
        with self.store.locked(target) as directory:
            credentials = self.store.credentials(directory)
            config = read_json(directory / '.config.json') or read_json(directory / '.claude.json')
            identity = config.get('oauthAccount', {})
            if identity.get('emailAddress', '').lower() != target or credentials.get('expiresAt', 0) < (self.clock() + 300) * 1000:
                raise ValueError('Dashboard login needs renewal before this switch.')
            return {'credentials': {k: credentials[k] for k in ('accessToken', 'expiresAt', 'scopes', 'subscriptionType', 'rateLimitTier') if k in credentials},
                    'identity': identity}

    def update(self, name, identifier, **fields):
        with self.lock:
            job = self.data['jobs'][name]
            if job['id'] != identifier or job['status'] == 'cancelled':
                return False
            job.update(fields)
            self.save()
            return True

    def tick(self, payload):
        with self.lock:
            jobs = copy.deepcopy(self.data['jobs'])
        for name, job in jobs.items():
            if job['status'] not in PENDING:
                continue
            if self.clock() - job['created_at'] > 12 * 3600:
                self.update(name, job['id'], status='expired', message='Switch expired after 12 hours. Reload and queue again.')
                continue
            try:
                check = self.runner(self.machines[name], {'action': 'probe'})
                # Recover a successful write whose SSH acknowledgement was lost.
                if check.get('account') == job['target'] and check.get('managed_request') == job['id']:
                    result = {'status': 'applied', 'expires_at': None}
                else:
                    self.allowed(payload, name, job['target'], self.clock())
                    if check.get('status') == 'waiting':
                        self.update(name, job['id'], status='waiting', active_sessions=check['active_sessions'],
                                    message=f"Waiting for {check['active_sessions']} open Claude processes to close (including idle terminals).")
                        continue
                    if check.get('status') != 'ready':
                        raise ValueError('Machine check failed; unlock Keychain if needed. Retrying.')
                    login = self.login(job['target'])
                    if not self.update(name, job['id'], status='applying', message='Applying login; keep Claude closed for a moment.'):
                        continue
                    result = self.runner(self.machines[name], {'action': 'apply', 'target': job['target'],
                        'expected_current': job['expected_current'], 'request_id': job['id'], 'login': login})
                self.update(name, job['id'], status=result['status'],
                            message='Switched. New Claude sessions use this account.' if result['status'] == 'applied' else result.get('message', 'Waiting for Claude to close.'),
                            active_sessions=result.get('active_sessions', 0))
                if result['status'] == 'applied':
                    with self.lock:
                        self.data['managed'][name] = {'account': job['target'], 'expires_at': result.get('expires_at'), 'status': 'connected'}
                        self.save()
            except ValueError as error:
                self.update(name, job['id'], status='blocked', message=str(error))
            except Exception:
                self.update(name, job['id'], status='blocked', message='Machine or login unavailable; retrying automatically.')
        with self.lock:
            managed_machines = copy.deepcopy(self.data['managed'])
        for name, managed in managed_machines.items():
            if managed.get('status') == 'conflict' or name not in self.machines:
                continue
            try:
                result = self.runner(self.machines[name], {'action': 'sync', 'target': managed['account'], 'login': self.login(managed['account'])})
                managed.update(status=result['status'], message=result.get('message', ''),
                               expires_at=result.get('expires_at', managed.get('expires_at')), checked_at=self.clock())
            except Exception:
                managed.update(status='retrying', message='Token sync unavailable; keep dashboard host online.', checked_at=self.clock())
            with self.lock:
                self.data['managed'][name] = managed
                self.save()
