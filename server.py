"""Dependency-free tailnet dashboard. Bind only to loopback or a Tailscale IP."""
import argparse
import concurrent.futures
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import subprocess
import shlex
import sys
import threading
import time
from accounts import AccountStore, EMAILS
from recommendations import Advisor
from notifications import AlertEngine
from settings import MACHINES, recommendation_settings

ROOT = Path(__file__).resolve().parent
state = {'machines': [], 'claude_accounts': [], 'updated_at': None, 'refreshing': True}
lock = threading.Lock()
advisor = Advisor()
alerts = AlertEngine()


def probe(machine):
    command = [sys.executable, str(ROOT / 'collector.py')]
    source = None
    if not machine.get('local'):
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                   '-o', 'StrictHostKeyChecking=yes', machine['ssh'], 'python3 -']
        source = (ROOT / 'collector.py').read_text()
    result = subprocess.run(command, input=source, text=True, capture_output=True, timeout=80)
    if result.returncode:
        raise RuntimeError('SSH or collector unavailable')
    return json.loads(result.stdout)


def poll(machines, interval):
    while True:
        with lock:
            state['refreshing'] = True
            previous = {m['name']: m for m in state['machines']}
        rows = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(machines) or 1) as pool:
            pending = [(m, pool.submit(probe, m)) for m in machines]
            for machine, future in pending:
                row = {'name': machine['name']}
                try:
                    row.update(future.result(), reachable=True)
                    # Preserve last successful quota separately for each provider and account.
                    for provider in ('claude', 'codex'):
                        reading = row[provider]
                        old = previous.get(machine['name'], {}).get(provider, {})
                        if reading.get('error'):
                            if reading.get('account') == old.get('account') and (old.get('session') or old.get('weekly')):
                                reading.update(session=old['session'], weekly=old.get('weekly'),
                                               observed_at=old.get('observed_at'), stale=True)
                        else:
                            reading['observed_at'] = row['collected_at']
                except Exception:
                    row = copy.deepcopy(previous.get(machine['name'], row))
                    row.update(reachable=False, error='Offline or SSH unavailable')
                    for provider in ('claude', 'codex'):
                        if provider in row:
                            row[provider]['stale'] = True
                rows.append(row)
        with lock:
            state.update(machines=rows, updated_at=time.time(), refreshing=False)
        time.sleep(interval)


def poll_accounts():
    store = AccountStore()
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            readings = list(pool.map(store.poll, EMAILS))
        with lock:
            state['claude_accounts'] = readings
        # AccountStore enforces per-account five-minute checks and backoff.
        # A completed login clears its gate so it appears promptly.
        time.sleep(15)


def poll_alerts():
    # Runs regardless of whether the dashboard is open.
    while True:
        try:
            with lock:
                payload = copy.deepcopy(state)
            config = recommendation_settings()
            payload['recommendations'] = advisor.evaluate(payload['machines'], payload['claude_accounts'], config)
            status = alerts.tick(payload)
            with lock:
                state['notifications'] = status
        except Exception:
            with lock:
                state['notifications'] = {'configured': False, 'enabled': False,
                                          'last_error': 'Notification check failed; retrying in one minute.'}
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/api/status':
            with lock:
                payload = copy.deepcopy(state)
            config = recommendation_settings()
            payload['recommendations'] = advisor.evaluate(payload['machines'], payload['claude_accounts'], config)
            payload['configured_accounts'] = list(EMAILS)
            payload['setup_commands'] = {name: 'python3 ' + shlex.quote(str(ROOT / (name + '.py')))
                                         for name in ('notifications', 'accounts')}
            payload.setdefault('notifications', alerts.public())
            body = json.dumps(payload).encode()
            mime = 'application/json'
        elif path == '/':
            body = (ROOT / 'index.html').read_bytes()
            mime = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--interval', type=int, default=300)
    args = parser.parse_args()
    address = ipaddress.ip_address(args.bind)
    if not (address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10')):
        parser.error('Use loopback or a Tailscale IPv4 address')
    machines = MACHINES
    state['machines'] = [{'name': m['name'], 'reachable': None} for m in machines]
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    threading.Thread(target=poll, args=(machines, max(300, args.interval)), daemon=True).start()
    threading.Thread(target=poll_accounts, daemon=True).start()
    threading.Thread(target=poll_alerts, daemon=True).start()
    print('Dashboard: http://' + args.bind + ':' + str(args.port), flush=True)
    server.serve_forever()
