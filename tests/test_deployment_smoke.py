import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit


spec = importlib.util.spec_from_file_location('deployment_smoke', Path(__file__).with_name('deployment-smoke.py'))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class Response(io.BytesIO):
    def __init__(self, body, code=200, api=True):
        super().__init__(body)
        self.code = code
        self.headers = {'Content-Type': 'application/json' if api else 'text/plain',
                        'Cache-Control': 'no-store' if api else 'public, max-age=0, must-revalidate',
                        'X-Content-Type-Options': 'nosniff',
                        'Content-Security-Policy': "frame-ancestors 'none'"}


class Client:
    def __init__(self):
        self.calls = []
        self.logged_in = False
        self.payload = {'books': [], 'settings': {}}

    def open(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        self.calls.append((method, path))
        status = 200
        if path == '/api/health':
            body = {'ok': True, 'authRequired': True}
        elif path == '/api/login':
            self.logged_in = True
            body = {'user': {'id': 'test-id', 'username': 'test_reader'}}
        elif path == '/api/logout':
            self.logged_in = False
            body = {'ok': True}
        elif path == '/api/nonexistent':
            status, body = 404, {'error': 'not found'}
        elif path.startswith('/api/') and not self.logged_in:
            status, body = 401, {'error': 'login required'}
        elif path in ('/api/bootstrap', '/api/state'):
            if method == 'POST':
                data = json.loads(request.data)
                status = 200 if data['version'] == 1 else 409
                body = {'ok': True, 'version': 1} if status == 200 else {'error': 'conflict'}
            else:
                body = {'version': 1, 'payload': self.payload,
                        'user': {'id': 'test-id', 'username': 'test_reader'}}
        elif path == '/api/search':
            body = {'results': []}
        else:
            body = 'static'
        raw = json.dumps(body).encode() if path.startswith('/api/') else body.encode()
        return Response(b'' if method == 'HEAD' else raw, status, path.startswith('/api/'))


class DeploymentSmokeTests(unittest.TestCase):
    def run_smoke(self, **extra):
        client = Client()
        output = io.StringIO()
        env = {'DEPLOYMENT_URL': 'https://example.test', **extra}
        cookies = [SimpleNamespace(secure=True, has_nonstandard_attr=lambda name: True)] * 2
        with patch.dict(os.environ, env, clear=True), patch.object(smoke, 'build_opener', return_value=client), patch.object(smoke.http.cookiejar, 'CookieJar', return_value=cookies), contextlib.redirect_stdout(output):
            smoke.main()
        return client.calls, output.getvalue()

    def test_default_checks_do_not_login_or_write_state(self):
        calls, _ = self.run_smoke()
        self.assertNotIn(('POST', '/api/state'), calls)
        self.assertNotIn(('POST', '/api/login'), calls)
        self.assertIn(('HEAD', '/storage.js'), calls)
        self.assertIn(('HEAD', '/api/health'), calls)

    def test_credentials_alone_do_not_enable_writes_or_search(self):
        calls, output = self.run_smoke(SMOKE_USERNAME='Test_Reader', SMOKE_PASSWORD='never-print-this-password')
        self.assertIn(('POST', '/api/login'), calls)
        self.assertIn(('POST', '/api/logout'), calls)
        self.assertNotIn(('POST', '/api/state'), calls)
        self.assertEqual(calls.count(('POST', '/api/search')), 1)  # Only the unauthenticated gate check.
        self.assertNotIn('never-print-this-password', output)
        self.assertNotIn('test-id', output)

    def test_explicit_test_account_write_checks_include_real_stale_version(self):
        calls, _ = self.run_smoke(SMOKE_USERNAME='test_reader', SMOKE_PASSWORD='test-password', SMOKE_WRITE_STATE='1')
        self.assertEqual(calls.count(('POST', '/api/state')), 2)

    def test_invalid_or_credential_origins_never_make_requests(self):
        for origin in ('http://example.test', 'https://user:secret@example.test',
                       'https://example.test/private', 'https://example.test?q=secret', 'https://example.test/#fragment'):
            with self.subTest(origin=origin), patch.dict(os.environ, {'DEPLOYMENT_URL': origin}, clear=True), patch.object(smoke, 'build_opener') as opener:
                with self.assertRaises(SystemExit):
                    smoke.main()
                opener.assert_not_called()
