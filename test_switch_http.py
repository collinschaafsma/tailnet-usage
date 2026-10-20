import io
import json
import unittest
from unittest.mock import patch

import server


class HTTPTests(unittest.TestCase):
    def post(self, token=None, origin='http://dashboard:8787', path='/api/switch'):
        body = json.dumps({'machine': 'worker', 'target': 'worker@example.com'}).encode()
        headers = {'Host': 'dashboard:8787', 'Origin': origin, 'Content-Length': str(len(body))}
        if token is not None:
            headers['X-CSRF-Token'] = token
        handler = object.__new__(server.Handler)
        handler.headers, handler.path = headers, path
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response = lambda status: setattr(handler, 'status', status)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.send_error = lambda status: setattr(handler, 'status', status)
        handler.do_POST()
        return handler.status

    def test_missing_csrf_cannot_queue(self):
        with patch.object(server.switches, 'queue') as queue:
            self.assertEqual(self.post(), 403)
            queue.assert_not_called()

    def test_cross_origin_cannot_queue_even_with_token(self):
        with patch.object(server.switches, 'queue') as queue:
            self.assertEqual(self.post(server.csrf_token, 'https://foreign.example'), 403)
            queue.assert_not_called()

    def test_authorized_same_origin_queues(self):
        with patch.object(server.switches, 'queue') as queue, patch.object(server, 'snapshot', return_value={}):
            self.assertEqual(self.post(server.csrf_token), 200)
            queue.assert_called_once_with('worker', 'worker@example.com', {})

    def test_authorized_cancel(self):
        with patch.object(server.switches, 'cancel') as cancel:
            self.assertEqual(self.post(server.csrf_token, path='/api/switch/cancel'), 200)
            cancel.assert_called_once_with('worker')
