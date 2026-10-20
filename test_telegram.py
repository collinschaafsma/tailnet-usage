import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from accounts import atomic_json, read_json
from notifications import AlertEngine, SendError, paired_chat, send_telegram, setup_telegram, telegram_call, validate
from test_notifications import snapshot, NOW

TOKEN = '123456789:' + 'A' * 35


def update(nonce='challenge', chat_type='private', sender=42, stamp=NOW):
    return {'update_id': 5, 'message': {'chat': {'id': 42, 'type': chat_type},
            'from': {'id': sender, 'is_bot': False}, 'text': '/start ' + nonce, 'date': stamp}}


class TelegramTests(unittest.TestCase):
    def test_pairing_requires_nonce_and_private_sender_identity(self):
        self.assertEqual(paired_chat([update()], 'challenge', NOW), '42')
        self.assertIsNone(paired_chat([update(nonce='wrong')], 'challenge', NOW))
        self.assertIsNone(paired_chat([update(chat_type='group')], 'challenge', NOW))
        self.assertIsNone(paired_chat([update(sender=99)], 'challenge', NOW))
        self.assertIsNone(paired_chat([update(stamp=NOW - 60)], 'challenge', NOW))

    def test_validation_disallows_group_or_missing_chat_and_invalid_token(self):
        config = {'provider': 'telegram', 'bot_token': TOKEN, 'chat_id': '42'}
        validate(config)
        for replacement in ({'chat_id': '-42'}, {'chat_id': ''}, {'bot_token': 'https://bad.example'}):
            with self.assertRaises(ValueError):
                validate({**config, **replacement})

    def test_send_uses_plain_text_to_paired_chat(self):
        def caller(token, method, params):
            self.assertEqual(token, TOKEN)
            self.assertEqual(method, 'sendMessage')
            self.assertEqual(params['chat_id'], '42')
            self.assertEqual(params['text'], '<plain text>')
            self.assertNotIn('parse_mode', params)
            self.assertTrue(params['link_preview_options']['is_disabled'])
            return {'message_id': 12}
        result = send_telegram({'provider': 'telegram', 'bot_token': TOKEN, 'chat_id': '42'}, '<plain text>', caller)
        self.assertEqual(result, {'sid': '12', 'status': 'sent'})

    def test_api_error_and_network_exception_never_expose_token(self):
        def reject(request, **kwargs):
            raise HTTPError(request.full_url, 401, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        def broken(request, **kwargs):
            raise OSError(request.full_url)
        for opener in (reject, broken):
            with self.assertRaises(SendError) as error:
                telegram_call(TOKEN, 'sendMessage', {}, opener)
            self.assertNotIn(TOKEN, str(error.exception))

    def test_api_posts_json_and_returns_provider_acknowledgement(self):
        def opener(request, **kwargs):
            self.assertEqual(request.full_url, 'https://api.telegram.org/bot' + TOKEN + '/sendMessage')
            self.assertEqual(request.method, 'POST')
            self.assertEqual(json.loads(request.data)['chat_id'], '42')
            return io.StringIO('{"ok":true,"result":{"message_id":3}}')
        self.assertEqual(telegram_call(TOKEN, 'sendMessage', {'chat_id': '42'}, opener)['message_id'], 3)

    def test_existing_webhook_is_not_deleted(self):
        methods = []
        def caller(token, method, params=None):
            methods.append(method)
            return {'is_bot': True, 'username': 'TailnetTestBot'} if method == 'getMe' else {'url': 'https://existing.example'}
        with patch('notifications.getpass.getpass', return_value=TOKEN), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                setup_telegram(caller)
        self.assertEqual(methods, ['getMe', 'getWebhookInfo'])

    def test_setup_pairs_only_challenge_and_stores_secret_privately(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            calls = []
            def caller(token, method, params=None):
                calls.append(method)
                if method == 'getMe':
                    return {'is_bot': True, 'username': 'TailnetTestBot'}
                if method == 'getWebhookInfo':
                    return {'url': ''}
                return [update(nonce='wrong'), update(nonce='tailnet_testnonce')]
            output = io.StringIO()
            with patch('notifications.CONFIG_PATH', path), patch('notifications.getpass.getpass', return_value=TOKEN), \
                    patch('notifications.secrets.token_urlsafe', return_value='testnonce'), \
                    patch('notifications.time.time', return_value=NOW), patch('notifications.AlertEngine') as engine, \
                    contextlib.redirect_stdout(output):
                engine.return_value.tick.return_value = {'last_error': None}
                setup_telegram(caller)
            config = read_json(path)
            self.assertEqual(config['chat_id'], '42')
            self.assertEqual(config['bot_token'], TOKEN)
            self.assertTrue(config['enabled'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn('https://t.me/TailnetTestBot?start=tailnet_testnonce', output.getvalue())

    def test_telegram_engine_reuses_alert_rules_and_redacts_token(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, state_path = Path(root) / 'config.json', Path(root) / 'events.json'
            atomic_json(config_path, {'provider': 'telegram', 'bot_token': TOKEN, 'chat_id': '42',
                                     'enabled': True, 'bot_username': 'TailnetTestBot'})
            sent = []
            def sender(config, body):
                sent.append(body)
                return {'sid': '4', 'status': 'sent'}
            engine = AlertEngine(config_path, state_path, clock=lambda: NOW, sender=sender)
            status = engine.tick(snapshot(low=True))
            self.assertEqual(len(sent), 1)
            self.assertEqual(status['provider'], 'telegram')
            self.assertEqual(status['recipient'], 'your private Telegram chat')
            self.assertEqual(status['history'][0]['status'], 'submitted')
            self.assertNotIn(TOKEN, json.dumps(status))
            self.assertNotIn(TOKEN, state_path.read_text())


if __name__ == '__main__':
    unittest.main()
