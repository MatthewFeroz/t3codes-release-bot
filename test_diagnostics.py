import contextlib
import datetime as dt
import io
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import bot
from api_diagnostics import MAX_ERROR_BYTES, error_summary


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Unmocked network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'snapshot.sqlite3'
        with patch.dict(os.environ, {'BOT_REMOTE_STATE': '0'}):
            store = bot.Store(self.path)
            store.queue({'release': {'id': 1, 'tag_name': 'nightly', 'published_at': '2026-10-06T00:00:00Z'}, 'posts': ['first', 'second']})
            with store.db:
                store.db.execute("UPDATE posts SET status='posted',tweet_id='100' WHERE position=0")
                store.db.execute("UPDATE posts SET status='sending',attempted='2026-10-07T00:42:16.123Z' WHERE position=1")
            store.db.close()

    def http_error(self, body, status=400):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return urllib.error.HTTPError('https://api.x.com/2/users/123/tweets?pagination_token=SECRET', status, 'SECRET', {'Authorization': 'SECRET'}, io.BytesIO(data))

    def test_error_redaction_and_bounded_metadata(self):
        body = {'title': 'Invalid Request', 'detail': 'SECRET email@example.com', 'token': 'SECRET',
                'errors': [{'parameters': {'start_time': ['SECRET'], 'secret_param': ['SECRET']},
                            'message': 'SECRET', 'code': 44}]}
        summary = error_summary(self.http_error(body), 'https://api.x.com/2/users/123/tweets?pagination_token=SECRET', 'GET')
        self.assertEqual(summary, {'method': 'GET', 'endpoint': '/2/users/:id/tweets', 'status': 400,
                                   'title': 'Invalid Request', 'error_count': 1, 'parameters': ['start_time'], 'codes': [44]})
        self.assertNotIn('SECRET', json.dumps(summary))
        for body in ({'title': ['SECRET'], 'error': {'SECRET': 1}}, {'title': 'SECRET', 'error': 'SECRET'}, [1], b'<html>SECRET</html>', b'x' * (MAX_ERROR_BYTES + 1)):
            self.assertNotIn('SECRET', json.dumps(error_summary(self.http_error(body), 'https://api.x.com/SECRET', 'GET')))

    def test_error_read_is_bounded(self):
        error = self.http_error(b'x' * 20000)
        with patch.object(error, 'read', wraps=error.read) as read:
            result = error_summary(error, 'https://api.x.com/2/tweets', 'POST')
        read.assert_called_once_with(MAX_ERROR_BYTES + 1)
        self.assertEqual(result['body'], 'oversized-omitted')

    def test_broken_error_body_preserves_status_and_omits_reason(self):
        error = self.http_error({}, 400)
        with patch.object(error, 'read', side_effect=http.client.IncompleteRead(b'SECRET')), \
             patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(bot.ApiError) as raised:
                bot.request_json('https://api.x.com/2/tweets')
        self.assertEqual(raised.exception.status, 400)
        self.assertNotIn('SECRET', str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_malformed_transport_never_logs_server_text(self):
        for error in (http.client.BadStatusLine('SECRET'), http.client.IncompleteRead(b'SECRET')):
            with patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, 'Network request failed') as raised:
                    bot.request_json('https://api.x.com/2/users/me')
            self.assertNotIn('SECRET', str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)

    def test_request_errors_include_method_and_redacted_path(self):
        for payload, method in ((None, 'GET'), ({'text': 'SECRET'}, 'POST')):
            with patch('urllib.request.urlopen', side_effect=self.http_error({'title': 'Forbidden'}, 403)):
                with self.assertRaises(bot.ApiError) as raised:
                    bot.request_json('https://api.x.com/2/tweets?secret=SECRET', headers={'Authorization': 'SECRET'}, payload=payload)
            self.assertEqual(raised.exception.status, 403)
            self.assertEqual(raised.exception.diagnostic['method'], method)
            self.assertNotIn('SECRET', str(raised.exception))

    def test_diagnostic_get_guard_blocks_writes_and_other_endpoints(self):
        api = bot.DiagnosticX()
        with patch.object(api, 'authenticate') as authenticate:
            for path, payload in [('tweets', {'text': 'SECRET'}), ('oauth2/token', None), ('tweets', None), ('users/me', {})]:
                with self.assertRaisesRegex(RuntimeError, 'only account'):
                    api.call(path, payload)
            with self.assertRaisesRegex(RuntimeError, 'cannot publish'):
                api.create('text', None)
            authenticate.assert_not_called()

    def run_snapshot(self, responses):
        before = self.path.read_bytes()
        files_before = set(Path(self.temp.name).iterdir())
        api = bot.DiagnosticX()
        api.token = 'TEST_ONLY'
        def response(request, **kwargs):
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return io.BytesIO(json.dumps(item).encode())
        output = io.StringIO()
        try:
            with patch('urllib.request.urlopen', side_effect=response) as opened, \
                 patch.object(bot.Store, '__init__', side_effect=AssertionError('Writable store forbidden')), \
                 patch.object(bot, 'publish', side_effect=AssertionError('Publish forbidden')), \
                 patch('subprocess.run', side_effect=AssertionError('Subprocess forbidden')), \
                 contextlib.redirect_stdout(output):
                bot.diagnose_reconciliation(self.path, api)
            return output.getvalue(), opened.call_args_list
        finally:
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(set(Path(self.temp.name).iterdir()), files_before)

    def test_success_keeps_state_identical_and_uses_exact_reply_parent(self):
        output, calls = self.run_snapshot([
            {'data': {'id': '123', 'username': 't3codes'}},
            {'data': [{'id': '200', 'text': 'second', 'referenced_tweets': [{'type': 'replied_to', 'id': '999'}]}], 'meta': {'next_token': 'SECRET'}},
            {'data': [{'id': '201', 'text': 'second', 'referenced_tweets': [{'type': 'replied_to', 'id': '100'}]}]},
        ])
        self.assertEqual(len(calls), 3)
        self.assertIn('matched; saved state unchanged', output)
        self.assertNotIn('SECRET', output)
        self.assertIn('start_time=2026-10-07T00%3A40%3A16.123000Z', calls[1].args[0].full_url)

    def test_no_match_and_http_failures_leave_state_identical(self):
        for response in ({'data': []}, self.http_error({'title': 'Invalid Request'}), self.http_error({}, 503)):
            with self.assertRaises(RuntimeError):
                self.run_snapshot([{'data': {'id': '123', 'username': 't3codes'}}, response])

    def test_401_cannot_refresh(self):
        with patch('cloud_auth.access_token', side_effect=AssertionError('Refresh forbidden')):
            with self.assertRaises(bot.ApiError) as raised:
                self.run_snapshot([self.http_error({}, 401)])
            self.assertEqual(raised.exception.status, 401)
            self.assertEqual(raised.exception.diagnostic['endpoint'], '/2/users/me')

    def test_no_uncertain_posts_needs_no_credentials(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE posts SET status='pending' WHERE status='sending'")
        db.close()
        with patch.object(bot.DiagnosticX, 'verify', side_effect=AssertionError('No API needed')), contextlib.redirect_stdout(io.StringIO()):
            bot.diagnose_reconciliation(self.path)

    def test_missing_snapshot_and_sidecars_fail_closed(self):
        with self.assertRaises(RuntimeError):
            bot.diagnose_reconciliation(self.path.with_name('missing'))
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.path) + suffix)
            sidecar.touch()
            with self.assertRaisesRegex(RuntimeError, 'sidecar'):
                bot.diagnose_reconciliation(self.path)
            sidecar.unlink()

    def test_command_bypasses_writable_main_paths(self):
        with patch.object(bot, 'diagnose_reconciliation') as diagnostic, \
             patch.object(bot, 'run_lock', side_effect=AssertionError('No lock writes')), \
             patch.object(bot, 'RotatingFileHandler', side_effect=AssertionError('No log writes')), \
             patch.object(bot.GitHub, 'releases', side_effect=AssertionError('No release discovery')):
            self.assertEqual(bot.main(['diagnose-reconciliation', '--state', str(self.path)]), 0)
            diagnostic.assert_called_once_with(self.path)

    def test_read_only_auth_never_refreshes_or_saves(self):
        import cloud_auth
        for offset, fails in ((60, True), (3600, False)):
            vault = {'access_token': 'TEST_ONLY', 'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset)).isoformat()}
            with patch.dict(os.environ, {'BOT_DATA_DIR': self.temp.name, 'X_VAULT_KEY': 'TEST_ONLY'}), \
                 patch.object(cloud_auth, 'Fernet') as fernet, \
                 patch.object(Path, 'read_bytes', return_value=b'TEST_ONLY'), \
                 patch.object(Path, 'write_bytes', side_effect=AssertionError('Vault writes forbidden')), \
                 patch.object(cloud_auth, 'checkpoint', side_effect=AssertionError('Checkpoint forbidden')):
                fernet.return_value.decrypt.return_value = json.dumps(vault).encode()
                if fails:
                    with self.assertRaisesRegex(RuntimeError, 'refresh is disabled'):
                        cloud_auth.access_token(read_only=True)
                else:
                    self.assertEqual(cloud_auth.access_token(read_only=True), 'TEST_ONLY')


if __name__ == '__main__':
    unittest.main()
