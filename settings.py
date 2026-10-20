"""Public defaults with private, ignored deployment configuration."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def load_settings(root=ROOT):
    private = root / 'config.local.json'
    path = private if private.is_file() else root / 'config.example.json'
    config = json.loads(path.read_text())
    accounts = config.get('claude_accounts', [])
    if not accounts or any(not isinstance(email, str) or not re.fullmatch(
            r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', email) for email in accounts):
        raise ValueError('Configure valid Claude account emails in config.local.json')
    config['claude_accounts'] = [email.lower() for email in accounts]
    if len(set(config['claude_accounts'])) != len(accounts):
        raise ValueError('Claude accounts must be unique')
    machines = config.get('machines', [])
    if not machines or any(not machine.get('name') or (not machine.get('local') and not machine.get('ssh')) for machine in machines):
        raise ValueError('Configure named machines and SSH destinations in config.local.json')
    return config


CONFIG = load_settings()
EMAILS = tuple(CONFIG['claude_accounts'])
MACHINES = CONFIG['machines']
DASHBOARD_URL = CONFIG.get('dashboard_url', 'http://localhost:8787')


def recommendation_settings():
    defaults = json.loads((ROOT / 'recommendations.json').read_text())
    return {**defaults, **CONFIG.get('recommendations', {})}
