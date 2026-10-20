"""Private Telegram/Twilio setup and persistent background alerts."""
import argparse
import base64
from contextlib import contextmanager
import datetime
import getpass
import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from accounts import STATE_DIR, atomic_json, read_json
from recommendations import number, timestamp
from settings import DASHBOARD_URL

CONFIG_PATH = STATE_DIR / 'notification-config.json'
EVENT_PATH = STATE_DIR / 'sms-events.json'


def configuration(path=CONFIG_PATH):
    config = read_json(path)
    if not config and path == CONFIG_PATH:
        config = read_json(STATE_DIR / 'sms-config.json')
    return config


@contextmanager
def state_lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def fresh(account, now):
    return (account.get('auth_status') == 'connected' and not account.get('error')
            and not account.get('stale') and number(account.get('observed_at'))
            and 0 <= now - account['observed_at'] <= 900)


def remaining(account, now):
    if not fresh(account, now):
        return None
    windows = [account.get(key) or {} for key in ('session', 'weekly')]
    if not all(number(w.get('used')) and 0 <= w['used'] <= 100 for w in windows):
        return None
    if any((reset := timestamp(w.get('reset'))) is not None and reset <= now for w in windows):
        return None
    return [100 - w['used'] for w in windows]


def event(kind, identity, body, priority=2):
    key = hashlib.sha256((kind + ':' + identity).encode()).hexdigest()[:24]
    return {'key': key, 'kind': kind, 'body': body, 'priority': priority}


def candidates(payload, saved, now):
    advice = payload.get('recommendations') or {}
    accounts = {a['account']: a for a in payload.get('claude_accounts', [])}
    events = []
    for row in advice.get('assignments', []):
        current = accounts.get(row.get('current'), {})
        quota = remaining(current, now)
        if quota is None:
            continue  # Never recommend a switch on unknown or stale quotas.
        short, weekly = quota
        director = row.get('role') == 'Director'
        # The account exhaustion alert below includes all affected worker moves.
        if not director and any(value <= 0 for value in quota):
            continue
        low = short < (advice.get('minimum_session_remaining', 30) if director else 10) or weekly < (advice.get('minimum_weekly_remaining', 20) if director else 5)
        target = row.get('recommended')
        if row.get('action') == 'Check machine':
            continue
        target_quota = remaining(accounts.get(target, {}), now)
        replacement = target and target != row.get('current') and target_quota is not None and all(value > 0 for value in target_quota)
        switch = (' Switch to ' + target + '.') if replacement else ' No safer replacement; pause new Claude work.'
        identity = row['machine'] + ':' + str(row.get('current')) + ':' + str(target)
        if director and low:
            events.append(event('director_low', identity,
                                f"{row['machine']} director: {short:.0f}% session, {weekly:.0f}% weekly left." + switch, 0))
        elif row.get('action') in ('Switch', 'Switch first') and replacement:
            # Director account isolation is actionable even before quota gets low.
            events.append(event('assignment', identity,
                                row['machine'] + ': switch Claude from ' + str(row.get('current')) +
                                ' to ' + target + ('. Reserve it for the director.' if director else '.'),
                                1 if director else 2))
        elif low and row.get('action') == 'Pause Claude':
            events.append(event('workers_blocked', identity, row['machine'] + ': Claude quota low or exhausted. Pause until an account recovers.', 1))
        elif low and not director:
            events.append(event('worker_low', identity,
                                f"{row['machine']}: {short:.0f}% session, {weekly:.0f}% weekly left." + switch, 2))
    known = saved.setdefault('account_states', {})
    recoveries = saved.setdefault('recoveries', {})
    for email, account in accounts.items():
        quota = remaining(account, now)
        if quota is not None:
            blocked = any(value <= 0 for value in quota)
            if blocked:
                windows = [key for key, value in zip(('session', 'weekly'), quota) if value <= 0]
                resets = [timestamp((account.get(key) or {}).get('reset')) for key in windows]
                reset = max(resets) if all(value is not None for value in resets) else None
                body = email + ' is exhausted (' + ' and '.join(windows) + ' limit reached).'
                if reset:
                    body += ' Resets ' + datetime.datetime.fromtimestamp(reset).astimezone().strftime('%a %b %d %I:%M %p %Z') + '.'
                moves = []
                for row in advice.get('assignments', []):
                    if row.get('current') != email or row.get('action') == 'Check machine':
                        continue
                    target = row.get('recommended')
                    available = remaining(accounts.get(target, {}), now)
                    moves.append(row['machine'] + (' -> ' + target if target != email and available is not None and all(value > 0 for value in available) else ': pause Claude'))
                if moves:
                    body += ' Switch: ' + '; '.join(moves) + '.'
                events.append(event('account_exhausted', email + ':' + ','.join(windows), body, 0))
            if known.get(email, {}).get('blocked') and not blocked:
                observed = account['observed_at']
                recoveries[email] = {'at': now, 'observed': observed}
            known[email] = {'blocked': blocked}
        if account.get('auth_status') in ('sign_in_required', 'wrong_account', 'renewal_paused'):
            events.append(event('login', email, email + ': dashboard login needs attention. Reconnect on desktop.', 1))
        elif account.get('auth_status') == 'connected':
            expires = timestamp(account.get('login_expires_at'))
            if expires and 0 < expires - now < 3 * 86400:
                events.append(event('login_expiring', email + ':' + str(int(expires)),
                                    email + ': dashboard sign-in expires within 3 days. Reconnect before expiry.', 2))
    for email, recovered in list(recoveries.items()):
        if now - recovered['at'] > 3600:
            del recoveries[email]
            continue
        quota = remaining(accounts.get(email, {}), now)
        if quota is not None and all(value > 0 for value in quota):
            events.append(event('recovered', email + ':' + str(recovered['observed']),
                                email + ': fresh reading confirms Claude quota is available again.', 3))
    return events


class SendError(Exception):
    def __init__(self, message, uncertain=False, retryable=False):
        super().__init__(message)
        self.uncertain = uncertain
        self.retryable = retryable


def validate(config):
    if config.get('provider') == 'telegram':
        if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,128}', config.get('bot_token', '')):
            raise ValueError('A valid Telegram bot token is required')
        if not re.fullmatch(r'[1-9][0-9]{0,19}', str(config.get('chat_id', ''))):
            raise ValueError('Pair your private Telegram chat first')
        return
    if config.get('provider', 'twilio') != 'twilio':
        raise ValueError('Unknown notification provider')
    if not re.fullmatch(r'AC[0-9a-fA-F]{32}', config.get('account_sid', '')):
        raise ValueError('A valid Twilio Account SID is required')
    if not re.fullmatch(r'[0-9a-fA-F]{32}', config.get('auth_token', '')):
        raise ValueError('A valid Twilio Auth Token is required')
    for name in ('from_number', 'to_number'):
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', config.get(name, '')):
            raise ValueError('Phone numbers must include country code, for example +13035550123')


def send_twilio(config, body, opener=urllib.request.urlopen):
    validate(config)
    credentials = base64.b64encode((config['account_sid'] + ':' + config['auth_token']).encode()).decode()
    data = urllib.parse.urlencode({'From': config['from_number'], 'To': config['to_number'], 'Body': body}).encode()
    request = urllib.request.Request('https://api.twilio.com/2010-04-01/Accounts/' + config['account_sid'] + '/Messages.json',
                                     data=data, headers={'Authorization': 'Basic ' + credentials,
                                     'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with opener(request, timeout=20) as response:
            result = json.load(response)
        if not result.get('sid'):
            raise SendError('Twilio response did not confirm submission; check Twilio Console.', uncertain=True)
        return {'sid': result['sid'], 'status': result.get('status', 'accepted')}
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        message = {401: 'Twilio credentials rejected.', 403: 'Twilio denied SMS; check sender and recipient setup.',
                   400: 'Twilio rejected SMS; check sender verification and recipient in Twilio Console.',
                   429: 'Twilio rate limited SMS.'}.get(code, 'Twilio could not confirm SMS submission.')
        raise SendError(message, uncertain=code >= 500, retryable=code == 429) from None
    except SendError:
        raise
    except Exception:
        # Timeout after POST may mean Twilio accepted it. Do not blindly resend.
        raise SendError('SMS submission outcome unknown; check Twilio Console.', uncertain=True) from None


def telegram_call(token, method, parameters=None, opener=urllib.request.urlopen):
    if method not in ('getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage'):
        raise ValueError('Unsupported Telegram method')
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(parameters or {}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with opener(request, timeout=25) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise SendError('Telegram rejected the request; check the bot token and whether the bot is blocked.')
        return result.get('result')
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        message = {401: 'Telegram bot token rejected; reconnect the bot.',
                   403: 'Telegram bot is blocked or cannot access the chat. Open it and tap Start.',
                   409: 'This bot already has another receiver. Use a dedicated bot for the dashboard.',
                   429: 'Telegram rate limited the request; retrying later.'}.get(code, 'Telegram request failed.')
        raise SendError(message, uncertain=code >= 500 and method == 'sendMessage', retryable=code == 429) from None
    except SendError:
        raise
    except Exception:
        # Telegram request URLs contain the token. Never expose raw exceptions.
        raise SendError('Telegram connection interrupted; outcome unknown.' if method == 'sendMessage'
                        else 'Could not reach Telegram; try setup again.', uncertain=method == 'sendMessage') from None


def send_telegram(config, body, caller=telegram_call):
    validate(config)
    result = caller(config['bot_token'], 'sendMessage', {
        'chat_id': str(config['chat_id']), 'text': body,
        'link_preview_options': {'is_disabled': True}})
    if not isinstance(result, dict) or not result.get('message_id'):
        raise SendError('Telegram did not confirm the message; check your bot chat.', uncertain=True)
    return {'sid': str(result['message_id']), 'status': 'sent'}


def send_notification(config, body):
    return send_telegram(config, body) if config.get('provider') == 'telegram' else send_twilio(config, body)


def paired_chat(updates, nonce, started_at):
    for update in updates:
        message = update.get('message') or {}
        chat, sender = message.get('chat') or {}, message.get('from') or {}
        if (chat.get('type') == 'private' and isinstance(chat.get('id'), int) and chat['id'] > 0
                and sender.get('id') == chat['id'] and not sender.get('is_bot')
                and message.get('text') == '/start ' + nonce
                and message.get('date', 0) >= started_at - 10):
            return str(chat['id'])
    return None


def setup_telegram(caller=telegram_call):
    print('Create a dedicated bot: open https://t.me/BotFather and send /newbot.')
    print('Paste its token below. It stays on desktop; it is not sent to the dashboard or chat.')
    token = getpass.getpass('Telegram bot token (hidden): ').strip()
    # Validate the token before placing it in an HTTP URL.
    validate({'provider': 'telegram', 'bot_token': token, 'chat_id': '1'})
    bot = caller(token, 'getMe')
    username = bot.get('username', '') if isinstance(bot, dict) else ''
    if not re.fullmatch(r'[A-Za-z0-9_]{5,64}', username) or not bot.get('is_bot'):
        raise ValueError('Telegram did not identify a valid bot')
    webhook = caller(token, 'getWebhookInfo')
    if not isinstance(webhook, dict) or webhook.get('url'):
        raise ValueError('This bot is connected elsewhere. Create a dedicated bot; its existing webhook was not changed.')
    nonce = 'tailnet_' + secrets.token_urlsafe(24)
    started_at = time.time()
    print('\nOpen this pairing link on YOUR phone and tap Start:', flush=True)
    print('https://t.me/' + username + '?start=' + nonce, flush=True)
    print('Waiting up to 5 minutes for your private chat. A confirmation will be sent when connected.', flush=True)
    offset = None
    while time.time() - started_at < 300:
        parameters = {'timeout': 10, 'allowed_updates': ['message'], 'limit': 100}
        if offset is not None:
            parameters['offset'] = offset
        updates = caller(token, 'getUpdates', parameters)
        if not isinstance(updates, list):
            raise ValueError('Telegram returned an unexpected pairing response')
        chat_id = paired_chat(updates, nonce, started_at)
        if chat_id:
            config = {'provider': 'telegram', 'bot_token': token, 'chat_id': chat_id,
                      'bot_username': username, 'enabled': True, 'daily_limit': 6,
                      'cooldown_seconds': 3600, 'rearm_at': time.time()}
            atomic_json(CONFIG_PATH, config)
            print('Telegram connected. Automatic alerts are enabled.', flush=True)
            result = AlertEngine().tick({}, test=True)
            if result.get('last_error'):
                print(result['last_error'])
            else:
                print('Confirmation sent. Check your Telegram bot chat.', flush=True)
            return
        for update in updates:
            if isinstance(update.get('update_id'), int):
                offset = max(offset or 0, update['update_id'] + 1)
    raise ValueError('Pairing timed out. Run setup again and open the new link.')


class AlertEngine:
    def __init__(self, config_path=CONFIG_PATH, state_path=EVENT_PATH, clock=time.time, sender=send_notification):
        self.config_path, self.state_path = config_path, state_path
        self.clock, self.sender = clock, sender
        self.lock = threading.Lock()
        self.saved = read_json(state_path)

    def public(self):
        config = configuration(self.config_path)
        try:
            validate(config)
            configured = True
        except ValueError:
            configured = False
        number = config.get('to_number', '')
        provider = config.get('provider', 'twilio' if config.get('account_sid') else 'telegram')
        return {'configured': configured, 'enabled': configured and bool(config.get('enabled')),
                'provider': provider,
                'recipient': ('your private Telegram chat' if provider == 'telegram' else 'ending in ' + number[-4:]) if configured else None,
                'bot_username': config.get('bot_username') if provider == 'telegram' and configured else None,
                'daily_limit': min(20, max(1, int(config.get('daily_limit', 6)))),
                'pending': self.saved.get('pending', 0),
                'last_error': self.saved.get('last_error'),
                'history': [{key: row[key] for key in ('at', 'body', 'status') if key in row}
                            for row in self.saved.get('history', [])[-5:]]}

    def tick(self, payload, test=False):
        with self.lock, state_lock(self.state_path):
            self.saved = read_json(self.state_path)
            now = self.clock()
            config = configuration(self.config_path)
            provider_name = 'Telegram' if config.get('provider') == 'telegram' else 'SMS'
            if config.get('rearm_at', 0) > self.saved.get('rearm_at', 0):
                self.saved.update(events={}, last_error=None, rearm_at=config['rearm_at'])
            for row in self.saved.get('history', []):
                if row.get('status') == 'sending':
                    row['status'] = 'uncertain'
                    self.saved['last_error'] = 'Alert submission was interrupted; check the destination before retrying.'
            if test:
                validate(config)
            ready = payload.get('updated_at') and payload.get('claude_accounts') and (payload.get('recommendations') or {}).get('assignments') is not None
            if not test and not ready:
                return self.public()
            events = [event('test', str(now), 'Tailnet usage: ' + provider_name + ' alerts are connected. Account-switch alerts are ready.', 0)] if test else candidates(payload, self.saved, now)
            tracked = self.saved.setdefault('events', {})
            live_keys = {item['key'] for item in events}
            for key in list(tracked):
                if key not in live_keys and now - tracked[key].get('last_seen', now) > 120:
                    del tracked[key]
            pending = []
            for item in events:
                record = tracked.setdefault(item['key'], {'first_seen': now})
                record['last_seen'] = now
                # An alert is sent once per continuous condition, across restarts.
                if record.get('status') in ('submitted', 'uncertain', 'sending', 'failed'):
                    continue
                if record.get('retry_at', 0) > now:
                    continue
                if test or item['priority'] == 0 or now - record['first_seen'] >= 120:
                    pending.append(item)
            self.saved['pending'] = len(pending)
            history = self.saved.setdefault('history', [])
            # Setup/test confirmation is not an operational alert. Recognize
            # older confirmations too, so existing installs recover immediately.
            def is_test(row):
                body = row.get('body', '')
                return row.get('is_test', False) or (body.startswith('Tailnet: Tailnet usage') and
                    ('alerts are connected.' in body or 'SMS test.' in body))
            recent = [row for row in history if row['at'] > now - 86400 and not is_test(row)]
            configured = self.public()['configured']
            enabled = configured and config.get('enabled')
            limit = min(20, max(1, int(config.get('daily_limit', 6))))
            cooldown = max(300, int(config.get('cooldown_seconds', 3600)))
            last_attempt = max((row['at'] for row in recent), default=0)
            critical = any(e['priority'] == 0 for e in pending)
            allowed = len(recent) < limit and (critical or now - last_attempt >= cooldown)
            if pending and (test or (enabled and allowed)):
                pending.sort(key=lambda item: (item['priority'], item['key']))
                # SMS stays short; Telegram can include the complete assignment plan.
                maximum_length = 3500 if config.get('provider') == 'telegram' else 300
                suffix = ' ' + DASHBOARD_URL
                selected, lines = [], []
                for item in pending:
                    text = item['body'].encode('ascii', 'replace').decode()
                    candidate = 'Tailnet: ' + ' '.join(lines + [text]) + suffix
                    if len(candidate) > maximum_length and selected:
                        break
                    selected.append(item)
                    lines.append(text)
                body = ('Tailnet: ' + ' '.join(lines))[:maximum_length - len(suffix)] + suffix
                row = {'at': now, 'body': body, 'status': 'sending', 'is_test': test,
                       'provider': config.get('provider', 'twilio')}
                history.append(row)
                for item in selected:
                    tracked[item['key']]['status'] = 'sending'
                # Durable outbox before POST: a crash must not cause duplicate SMS.
                atomic_json(self.state_path, self.saved)
                try:
                    submitted = self.sender(config, body)
                    row.update(status='submitted', provider_status=submitted.get('status'), sid=submitted.get('sid'))
                    self.saved['last_error'] = None
                except SendError as error:
                    row['status'] = 'uncertain' if error.uncertain else 'retry_later' if error.retryable else 'failed'
                    self.saved['last_error'] = str(error)
                except Exception:
                    row['status'] = 'uncertain'
                    self.saved['last_error'] = 'Alert submission outcome unknown; check the destination before retrying.'
                for item in selected:
                    tracked[item['key']]['status'] = row['status']
                    if row['status'] == 'retry_later':
                        tracked[item['key']]['retry_at'] = now + 3600
                self.saved['pending'] = max(0, len(pending) - len(selected))
            self.saved['history'] = history[-100:]
            atomic_json(self.state_path, self.saved)
            return self.public()


def setup():
    print('Create your Twilio account at https://www.twilio.com/try-twilio')
    print('Complete SMS sender setup and verify the receiving number if using a trial.')
    print('Values below are stored privately on desktop, never in the dashboard or source.')
    print('Saving this setup enables automatic SMS alerts (maximum 6 submissions per 24 hours).')
    config = {'provider': 'twilio', 'account_sid': getpass.getpass('Twilio Account SID (hidden): ').strip(),
              'auth_token': getpass.getpass('Twilio Auth Token (hidden): ').strip(),
              'from_number': getpass.getpass('Twilio sender number, +countrycode (hidden): ').strip(),
              'to_number': getpass.getpass('Your receiving number, +countrycode (hidden): ').strip(),
              'enabled': True, 'daily_limit': 6, 'cooldown_seconds': 3600, 'rearm_at': time.time()}
    validate(config)
    atomic_json(CONFIG_PATH, config)
    print('SMS alerts enabled. Use `python3 notifications.py test` to send one test text.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'setup-telegram', 'setup-sms', 'status', 'test', 'enable', 'disable', 'retry'])
    args = parser.parse_args()
    try:
        if args.command in ('setup', 'setup-telegram'):
            setup_telegram()
        elif args.command == 'setup-sms':
            setup()
        elif args.command in ('enable', 'disable'):
            config = configuration()
            if args.command == 'enable':
                validate(config)
            config['enabled'] = args.command == 'enable'
            atomic_json(CONFIG_PATH, config)
            print('Alerts ' + ('enabled' if config['enabled'] else 'disabled'))
        elif args.command == 'retry':
            config = configuration()
            config['rearm_at'] = time.time()
            atomic_json(CONFIG_PATH, config)
            print('Alert conditions rearmed; regular cooldown and daily limits still apply.')
        else:
            engine = AlertEngine()
            status = engine.tick({}, test=True) if args.command == 'test' else engine.public()
            print(json.dumps(status, indent=2))
    except (ValueError, SendError, KeyboardInterrupt, EOFError) as error:
        print('Setup incomplete.' if isinstance(error, (KeyboardInterrupt, EOFError)) else str(error))
        raise SystemExit(1)
