"""Opt-in RPC, bootstrap, budgets and HTTP checks; never contact external services."""
import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cloud
import observability
import reader
from test_reader import BASE, chapter


UID = '12345678-1234-1234-1234-123456789012'
NAME = '测试读者'
AUTH_USER = {'id': UID, 'email': cloud.username_email(NAME), 'user_metadata': {'username': NAME}}
USER = {'id': UID, 'username': NAME}
PAYLOAD = {'books': [], 'settings': {}}
STATE = {'version': 2, 'payload': PAYLOAD}
SESSION = {'access_token': 'new', 'refresh_token': 'new-refresh', 'expires_in': 3600}
HEADERS = {'Cookie': 'pn_access=access; pn_refresh=refresh'}
CONTEXT = '/rest/v1/rpc/reader_context_v2'
GATE = '/rest/v1/rpc/reader_gate_v2'
SAVE = '/rest/v1/rpc/reader_save_v2'


class RPCFlowTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'READER_RPC_V2': '1'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_bootstrap_v2_is_auth_plus_user_jwt_context(self):
        deadline = time.monotonic() + 45
        with patch('cloud.remote', side_effect=[AUTH_USER, STATE]) as remote:
            result = cloud.context(HEADERS, [], deadline=deadline)
        self.assertEqual(result, {'user': USER, **STATE})
        self.assertEqual([call.args[0] for call in remote.call_args_list], ['/auth/v1/user', CONTEXT])
        self.assertTrue(all(call.kwargs['token'] == 'access' and call.kwargs['deadline'] == deadline for call in remote.call_args_list))
        self.assertEqual(remote.call_args_list[1].kwargs['data'], {})
        self.assertTrue(all('admin' not in call.kwargs for call in remote.call_args_list))

    def test_bootstrap_legacy_is_default_and_three_calls(self):
        for flag in ('', '0', 'true', 'yes'):
            with self.subTest(flag=flag), patch.dict(os.environ, {'READER_RPC_V2': flag}), patch('cloud.remote', side_effect=[AUTH_USER, [{'user_id': UID}], [STATE]]) as remote:
                self.assertEqual(cloud.context(HEADERS, []), {'user': USER, **STATE})
                self.assertEqual(remote.call_count, 3)
                self.assertIn('/members?', remote.call_args_list[1].args[0])
                self.assertTrue(remote.call_args_list[2].args[0].startswith('/rest/v1/reader_states?'))

    def test_legacy_session_does_not_fetch_state(self):
        with patch.dict(os.environ, {}, clear=True), patch('cloud.remote', side_effect=[AUTH_USER, [{}]]) as remote:
            self.assertEqual(cloud.context(HEADERS, [], include_state=False), {'user': USER})
        self.assertEqual(remote.call_count, 2)

    def test_v2_gate_has_two_calls_and_no_browser_uid(self):
        for route in ('search', 'read', 'check'):
            with self.subTest(route=route), patch('cloud.remote', side_effect=[AUTH_USER, True]) as remote:
                self.assertEqual(cloud.authorize(HEADERS, [], route), (USER, 'access'))
                remote.assert_called_with(GATE, token='access', data={'p_route': route})
                self.assertEqual(remote.call_count, 2)

    def test_v2_sync_has_two_calls_instead_of_four(self):
        for flag, replies, expected in (
            ('1', [AUTH_USER, {'ok': True, 'version': 2}], ['/auth/v1/user', SAVE]),
            ('0', [AUTH_USER, [{}], True, {'ok': True, 'version': 2}],
             ['/auth/v1/user', '/rest/v1/members?', '/rest/v1/rpc/consume_reader_quota', '/rest/v1/rpc/save_reader_state']),
        ):
            with self.subTest(flag=flag), patch.dict(os.environ, {'READER_RPC_V2': flag}), patch('cloud.remote', side_effect=replies) as remote:
                self.assertEqual(cloud.save(HEADERS, [], STATE), {'ok': True, 'version': 2})
                self.assertEqual(remote.call_count, len(expected))
                for call, prefix in zip(remote.call_args_list, expected):
                    self.assertTrue(call.args[0].startswith(prefix))
                if flag == '0':
                    self.assertTrue(remote.call_args_list[2].kwargs['admin'])

    def test_chinese_login_verifies_auth_and_context_before_cookies(self):
        outgoing = []
        with patch.dict(os.environ, {'COOKIE_SECURE': '1'}), patch('cloud.remote', side_effect=[SESSION, AUTH_USER, STATE]) as remote:
            self.assertEqual(cloud.login({'username': NAME, 'password': 'test-password'}, outgoing), {'user': USER})
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(remote.call_args_list[1].args[0], '/auth/v1/user')
        self.assertTrue(all('HttpOnly' in value and 'Secure' in value for _, value in outgoing))

    def test_refresh_revalidates_user_then_membership_rpc(self):
        outgoing = []
        with patch('cloud.remote', side_effect=[cloud.CloudError(401, 'expired'), SESSION, AUTH_USER, STATE]) as remote:
            self.assertEqual(cloud.context(HEADERS, outgoing), {'user': USER, **STATE})
        self.assertEqual([call.args[0] for call in remote.call_args_list],
                         ['/auth/v1/user', '/auth/v1/token?grant_type=refresh_token', '/auth/v1/user', CONTEXT])
        self.assertEqual(remote.call_args_list[-1].kwargs['token'], 'new')
        self.assertEqual(len(outgoing), 2)

    def test_refresh_denied_membership_sets_no_cookies(self):
        for operation in ('context', 'gate', 'save'):
            outgoing = []
            with self.subTest(operation=operation), patch('cloud.remote', side_effect=[SESSION, AUTH_USER, cloud.CloudError(403, 'inactive')]) as remote:
                with self.assertRaises(cloud.CloudError) as error:
                    if operation == 'context':
                        cloud.context({'Cookie': 'pn_refresh=refresh'}, outgoing)
                    elif operation == 'gate':
                        cloud.authorize({'Cookie': 'pn_refresh=refresh'}, outgoing, 'read')
                    else:
                        cloud.save({'Cookie': 'pn_refresh=refresh'}, outgoing, STATE)
                self.assertEqual(error.exception.status, 403)
                self.assertEqual(outgoing, [])
                self.assertEqual(remote.call_count, 3)

    def test_refreshed_session_survives_conflict_and_quota_denial(self):
        for operation, response, status in (
            ('save', {'ok': False, 'version': 3}, 409),
            ('save', cloud.CloudError(429, 'quota'), 429),
            ('save', cloud.CloudError(400, 'invalid SQL payload'), 400),
            ('save', cloud.CloudError(413, 'SQL payload size'), 413),
            ('gate', cloud.CloudError(400, 'invalid route'), 400),
            ('gate', cloud.CloudError(429, 'quota'), 429),
        ):
            outgoing = []
            with self.subTest(operation=operation, status=status), patch('cloud.remote', side_effect=[SESSION, AUTH_USER, response]) as remote:
                with self.assertRaises(cloud.CloudError) as error:
                    if operation == 'save':
                        cloud.save({'Cookie': 'pn_refresh=refresh'}, outgoing, STATE)
                    else:
                        cloud.authorize({'Cookie': 'pn_refresh=refresh'}, outgoing, 'read')
                self.assertEqual(error.exception.status, status)
                self.assertEqual(remote.call_count, 3)
                self.assertEqual(len(outgoing), 2)
                self.assertIn('pn_refresh=new-refresh;', outgoing[1][1])

    def test_non_auth_failures_never_refresh(self):
        for status in (400, 403, 409, 413, 429, 503):
            with self.subTest(status=status), patch('cloud.remote', side_effect=cloud.CloudError(status, 'failure')) as remote:
                with self.assertRaises(cloud.CloudError) as error:
                    cloud.context(HEADERS, [])
                self.assertEqual(error.exception.status, status)
                self.assertEqual(remote.call_count, 1)

    def test_missing_or_denied_rpc_never_falls_back(self):
        for status in (400, 401, 403, 429, 503):
            for operation in ('context', 'gate', 'save'):
                with self.subTest(status=status, operation=operation), patch('cloud.remote', side_effect=[AUTH_USER, cloud.CloudError(status, 'RPC failed')]) as remote:
                    with self.assertRaises(cloud.CloudError) as error:
                        if operation == 'context':
                            cloud.context(HEADERS, [])
                        elif operation == 'gate':
                            cloud.authorize(HEADERS, [], 'read')
                        else:
                            cloud.save(HEADERS, [], STATE)
                    self.assertEqual(error.exception.status, status)
                    self.assertEqual(remote.call_count, 2)

    def test_authoritative_auth_denial_never_reaches_rpc(self):
        with patch('cloud.remote', side_effect=cloud.CloudError(401, 'revoked')) as remote:
            with self.assertRaises(cloud.CloudError) as error:
                cloud.authorize({'Cookie': 'pn_access=revoked'}, [], 'read')
        self.assertEqual(error.exception.status, 401)
        remote.assert_called_once_with('/auth/v1/user', token='revoked')

    def test_membership_is_not_cached(self):
        with patch('cloud.remote', side_effect=[AUTH_USER, STATE, AUTH_USER, cloud.CloudError(403, 'disabled')]) as remote:
            cloud.context(HEADERS, [])
            with self.assertRaises(cloud.CloudError) as error:
                cloud.context(HEADERS, [])
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(remote.call_count, 4)

    def test_invalid_rpc_context_fails_closed(self):
        for state in (None, {}, {'version': True, 'payload': None}, {'version': -1, 'payload': None}, {'version': 0, 'payload': []}):
            with self.subTest(state=state), patch('cloud.remote', side_effect=[AUTH_USER, state]):
                with self.assertRaises(cloud.CloudError) as error:
                    cloud.context(HEADERS, [])
                self.assertEqual(error.exception.status, 502)

    def test_each_cloud_step_inherits_exact_deadline(self):
        for operation in ('context', 'gate', 'save', 'login'):
            now, seen = [100.0], []

            def remote(path, **kwargs):
                seen.append(kwargs['deadline'])
                now[0] += 5
                if path.startswith('/auth/v1/token'):
                    return SESSION
                if path == '/auth/v1/user':
                    return AUTH_USER
                if path == GATE:
                    return True
                if path == SAVE:
                    return {'ok': True, 'version': 2}
                return STATE

            with self.subTest(operation=operation), patch('cloud.budget.time.monotonic', side_effect=lambda: now[0]), patch('cloud.remote', side_effect=remote):
                if operation == 'context':
                    cloud.context({'Cookie': 'pn_refresh=refresh'}, [], deadline=145)
                elif operation == 'gate':
                    cloud.authorize(HEADERS, [], 'search', deadline=145)
                elif operation == 'save':
                    cloud.save(HEADERS, [], STATE, deadline=145)
                else:
                    cloud.login({'username': NAME, 'password': 'test'}, [], deadline=145)
            self.assertTrue(seen)
            self.assertEqual(seen, [145] * len(seen))

    def test_expired_auth_never_starts_business_rpc(self):
        for operation in ('context', 'gate', 'save', 'login'):
            now = [100.0]

            def remote(*args, **kwargs):
                now[0] = 146
                return AUTH_USER

            with self.subTest(operation=operation), patch('cloud.budget.time.monotonic', side_effect=lambda: now[0]), patch('cloud.remote', side_effect=remote) as call:
                with self.assertRaises(cloud.CloudError) as error:
                    if operation == 'context':
                        cloud.context(HEADERS, [], deadline=145)
                    elif operation == 'gate':
                        cloud.authorize(HEADERS, [], 'read', deadline=145)
                    elif operation == 'save':
                        cloud.save(HEADERS, [], STATE, deadline=145)
                    else:
                        cloud.login({'username': NAME, 'password': 'test'}, [], deadline=145)
                self.assertEqual(error.exception.status, 504)
                self.assertEqual(call.call_count, 1)


class RemoteStatusTests(unittest.TestCase):
    def test_http_status_mapping_preserves_business_failures(self):
        for status in (400, 401, 403, 409, 413, 429, 404, 500):
            response = HTTPError('https://cloud.invalid/rpc', status, 'private upstream details', {}, io.BytesIO(b'{}'))
            with self.subTest(status=status), patch('cloud.config', return_value=('https://cloud.invalid', 'key')), patch('cloud.urlopen', side_effect=response):
                with self.assertRaises(cloud.CloudError) as error:
                    cloud.remote(SAVE, token='test', data={})
                self.assertEqual(error.exception.status, status if status in (400, 401, 403, 409, 413, 429) else 503)
                self.assertNotIn('private upstream details', str(error.exception))

    def test_old_gotrue_invalid_credentials_400_is_path_specific(self):
        for path, body, expected in (
            ('/auth/v1/token?grant_type=password', {'error_code': 'invalid_credentials'}, 401),
            ('/auth/v1/token?grant_type=refresh_token', {'error': 'invalid_grant'}, 401),
            ('/auth/v1/token?grant_type=password', {'error_code': 'validation_failed'}, 400),
            (SAVE, {'error_code': 'invalid_credentials'}, 400),
        ):
            response = HTTPError('https://cloud.invalid', 400, 'bad', {}, io.BytesIO(json.dumps(body).encode()))
            with self.subTest(path=path, body=body), patch('cloud.config', return_value=('https://cloud.invalid', 'key')), patch('cloud.urlopen', side_effect=response):
                with self.assertRaises(cloud.CloudError) as error:
                    cloud.remote(path, data={})
                self.assertEqual(error.exception.status, expected)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'READER_RPC_V2': '1'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_utf8_500000_boundary_matches_jsonb_spaces(self):
        payload = {'books': [], 'settings': {'text': ''}}
        overhead = cloud.payload_size(payload)
        payload['settings']['text'] = '中' * 100000 + 'a' * (500000 - overhead - 300000)
        self.assertEqual(cloud.payload_size(payload), 500000)
        with patch('cloud.remote', return_value={'ok': True, 'version': 1}) as remote:
            cloud.sync('token', {'version': 0, 'payload': payload})
            self.assertEqual(remote.call_count, 1)
            payload['settings']['text'] += 'a'
            with self.assertRaises(cloud.CloudError) as error:
                cloud.sync('token', {'version': 0, 'payload': payload})
            self.assertEqual(error.exception.status, 413)
            self.assertEqual(remote.call_count, 1)

    def test_200_books_accepted_201_rejected(self):
        data = {'version': 0, 'payload': {'books': [{}] * 200, 'settings': {}}}
        with patch('cloud.remote', return_value={'ok': True, 'version': 1}) as remote:
            cloud.sync('token', data)
            data['payload']['books'].append({})
            with self.assertRaises(cloud.CloudError) as error:
                cloud.sync('token', data)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(remote.call_count, 1)

    def test_jsonb_size_uses_expanded_numbers_unicode_and_escapes(self):
        for value, text in ((1e-7, '0.0000001'), (1e20, '100000000000000000000'),
                            (-0.0, '0.0'), (True, 'true'), (None, 'null'),
                            ({'n': 1e-7}, '{"n": 0.0000001}'),
                            ({'中': ['文字', '\n\t"\\']}, json.dumps({'中': ['文字', '\n\t"\\']}, ensure_ascii=False))):
            with self.subTest(value=value):
                self.assertEqual(cloud.payload_size(value), len(text.encode('utf-8')))

    def test_unrepresentable_values_and_invalid_versions_do_not_call_rpc(self):
        invalid = [float('nan'), float('inf'), '\x00', '\ud800']
        with patch('cloud.remote') as remote:
            for value in invalid:
                with self.subTest(value=repr(value)), self.assertRaises(cloud.CloudError) as error:
                    cloud.sync('token', {'version': 0, 'payload': {'books': [], 'settings': {'value': value}}})
                self.assertEqual(error.exception.status, 400)
            for version in (True, -1, 2 ** 63):
                with self.subTest(version=version), self.assertRaises(cloud.CloudError):
                    cloud.sync('token', {'version': version, 'payload': PAYLOAD})
            remote.assert_not_called()

    def test_invalid_v2_payload_is_rejected_before_rotating_refresh(self):
        outgoing = []
        data = {'version': 0, 'payload': {'books': [{}] * 201, 'settings': {}}}
        with patch('cloud.remote') as remote:
            with self.assertRaises(cloud.CloudError) as error:
                cloud.save({'Cookie': 'pn_refresh=refresh'}, outgoing, data)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(outgoing, [])
            remote.assert_not_called()

    def test_noop_version_is_returned_and_stale_cas_is_conflict(self):
        with patch('cloud.remote', side_effect=[{'ok': True, 'version': 2}, {'ok': False, 'version': 2}]):
            self.assertEqual(cloud.sync('token', STATE), {'ok': True, 'version': 2})
            with self.assertRaises(cloud.CloudError) as error:
                cloud.sync('token', {'version': 1, 'payload': PAYLOAD})
        self.assertEqual(error.exception.status, 409)


class AdditionalSourceBudgetTests(unittest.TestCase):
    def test_openbook_dns_timeout_retains_capacity_and_never_connects_late(self):
        entered, release = threading.Event(), threading.Event()
        workers = reader.budget.BoundedWorkers(1, 'test-openbook-dns')

        def addresses(host, port):
            entered.set()
            release.wait(2)
            return ['93.184.216.34']

        with patch.object(reader.budget, 'NETWORK_WORKERS', workers), patch('reader.public_addresses', side_effect=addresses), patch('reader.PinnedConnection') as connection:
            try:
                start = time.monotonic()
                with self.assertRaises(reader.ReaderTimeout):
                    reader.acquire_openbook('pg-23962', start + 0.05)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.5)
                with self.assertRaisesRegex(reader.ReaderError, '其他请求'):
                    reader.acquire_openbook('pg-23962', time.monotonic() + 1)
            finally:
                release.set()
                self.assertTrue(workers.slots.acquire(timeout=1))
                workers.slots.release()
            connection.assert_not_called()


class OnlineHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = reader.ThreadingHTTPServer(('127.0.0.1', 0), reader.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        reader.CACHE.clear()

    def test_local_bootstrap_and_legacy_endpoints(self):
        with patch('cloud.remote') as remote:
            for path in ('/api/bootstrap', '/api/session', '/api/state'):
                with self.subTest(path=path), urlopen(self.base + path) as response:
                    self.assertEqual(json.load(response), {'local': True})
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
            remote.assert_not_called()

    def test_bootstrap_contract_for_both_flags(self):
        for flag in ('0', '1'):
            replies = [AUTH_USER, STATE] if flag == '1' else [AUTH_USER, [{}], [STATE]]
            with self.subTest(flag=flag), patch.dict(os.environ, {'REQUIRE_AUTH': '1', 'READER_RPC_V2': flag}), patch('cloud.remote', side_effect=replies) as remote:
                with urlopen(Request(self.base + '/api/bootstrap', headers=HEADERS)) as response:
                    self.assertEqual(json.load(response), {'user': USER, **STATE})
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual(remote.call_count, 2 if flag == '1' else 3)

    def test_head_static_and_health_send_headers_without_body(self):
        for path in ('/', '/app.js', '/ebooks.js', '/storage.js', '/style.css', '/sw.js', '/manifest.webmanifest', '/icon.svg', '/api/health'):
            with self.subTest(path=path), urlopen(self.base + path) as response:
                data = response.read()
                content_type = response.headers['Content-Type']
                old_id = response.headers['X-Request-ID']
            with urlopen(Request(self.base + path, method='HEAD', headers={'X-Request-ID': 'untrusted'})) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'')
                self.assertEqual(response.headers['Content-Length'], str(len(data)))
                self.assertEqual(response.headers['Content-Type'], content_type)
                self.assertRegex(response.headers['X-Request-ID'], r'^[0-9a-f]{32}$')
                self.assertNotEqual(response.headers['X-Request-ID'], old_id)
                self.assertIn('total;dur=', response.headers['Server-Timing'])

    def test_business_head_does_not_authenticate_or_consume(self):
        with patch.dict(os.environ, {'REQUIRE_AUTH': '1'}), patch('cloud.remote') as remote:
            for path in ('/api/bootstrap', '/api/session', '/api/state', '/api/read', '/api/search', '/api/check', '/api/login', '/api/logout'):
                with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                    urlopen(Request(self.base + path, method='HEAD'))
                self.assertEqual(error.exception.code, 405)
                self.assertEqual(error.exception.read(), b'')
                self.assertIn('Allow', error.exception.headers)
            remote.assert_not_called()

    def test_unknown_api_is_404_for_get_head_and_post(self):
        for method in ('GET', 'HEAD', 'POST'):
            with self.subTest(method=method), self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base + '/api/not-present', method=method))
            self.assertEqual(error.exception.code, 404)
            self.assertEqual(error.exception.headers['Cache-Control'], 'no-store')
            self.assertIn('X-Request-ID', error.exception.headers)
            if method == 'HEAD':
                self.assertEqual(error.exception.read(), b'')

    def test_all_account_routes_pass_one_shared_deadline(self):
        def remote(path, **kwargs):
            if path.startswith('/auth/v1/token'):
                return SESSION
            if path == '/auth/v1/user':
                return AUTH_USER
            if path == CONTEXT:
                return STATE
            if path == SAVE:
                return {'ok': True, 'version': 2}
            return {}

        routes = [('/api/bootstrap', None), ('/api/session', None), ('/api/state', None),
                  ('/api/login', {'username': NAME, 'password': 'password'}),
                  ('/api/logout', {}), ('/api/state', STATE)]
        for path, body in routes:
            with self.subTest(path=path, body=body), patch.dict(os.environ, {'REQUIRE_AUTH': '1', 'READER_RPC_V2': '1'}), patch('cloud.enabled', return_value=True), patch('cloud.remote', side_effect=remote) as call:
                data = json.dumps(body).encode() if body is not None else None
                headers = {**HEADERS, 'Content-Type': 'application/json'}
                start = time.monotonic()
                with urlopen(Request(self.base + path, data=data, headers=headers)) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
                deadlines = [item.kwargs['deadline'] for item in call.call_args_list]
                self.assertTrue(deadlines)
                self.assertTrue(all(start < deadline <= time.monotonic() + 45 for deadline in deadlines))
                self.assertEqual(len(set(deadlines)), 1)

    def test_rpc_denial_does_not_fetch_or_refresh(self):
        for status in (400, 401, 403, 429, 503):
            req = Request(self.base + '/api/read', data=json.dumps({'url': BASE}).encode(),
                          headers={**HEADERS, 'Content-Type': 'application/json'})
            with self.subTest(status=status), patch.dict(os.environ, {'REQUIRE_AUTH': '1', 'READER_RPC_V2': '1'}), patch('cloud.remote', side_effect=[AUTH_USER, cloud.CloudError(status, 'denied')]) as remote, patch('reader.load_reading') as source:
                with self.assertRaises(HTTPError) as error:
                    urlopen(req)
                self.assertEqual(error.exception.code, status)
                self.assertEqual(remote.call_count, 2)
                source.assert_not_called()

    def test_sync_http_preserves_business_status(self):
        for status in (400, 403, 409, 413, 429):
            req = Request(self.base + '/api/state', data=json.dumps(STATE).encode(),
                          headers={**HEADERS, 'Content-Type': 'application/json'})
            with self.subTest(status=status), patch.dict(os.environ, {'REQUIRE_AUTH': '1', 'READER_RPC_V2': '1'}), patch('cloud.remote', side_effect=[AUTH_USER, cloud.CloudError(status, 'denied')]):
                with self.assertRaises(HTTPError) as error:
                    urlopen(req)
                self.assertEqual(error.exception.code, status)

    def test_request_logs_and_worker_fetch_timing_are_metadata_only(self):
        captured, finished = io.StringIO(), threading.Event()
        original = observability.Trace.finish

        def finish(trace, status):
            original(trace, status)
            finished.set()

        req = Request(self.base + '/api/read?private=SECRET_QUERY',
                      data=json.dumps({'url': BASE + '?SECRET_BODY'}).encode(),
                      headers={**HEADERS, 'Content-Type': 'application/json', 'X-Request-ID': 'SECRET_HEADER'})
        with patch.dict(os.environ, {'REQUIRE_AUTH': '1', 'READER_RPC_V2': '1', 'READER_LOG_REQUESTS': '1'}), redirect_stderr(captured), patch('cloud._remote', side_effect=[AUTH_USER, True]), patch('reader.fetch_page', return_value=(chapter(), BASE)), patch.object(observability.Trace, 'finish', finish):
            with urlopen(req) as response:
                self.assertEqual(response.status, 200)
                response.read()
                self.assertIn('cloud_calls;desc="2"', response.headers['Server-Timing'])
            self.assertTrue(finished.wait(1))
        text = captured.getvalue()
        self.assertNotIn('SECRET', text)
        self.assertNotIn('pn_access', text)
        record = json.loads(text)
        self.assertEqual(record['route'], '/api/read')
        self.assertEqual(record['status'], 200)
        self.assertEqual(record['cloud_count'], 2)
        self.assertEqual(record['rpc_count'], 1)
        self.assertEqual(record['fetch_count'], 1)
        self.assertGreaterEqual(record['fetch_ms'], 0)

    def test_publications_fetch_inherits_auth_budget(self):
        now, seen = [100.0], []

        def remote(path, **kwargs):
            seen.append(kwargs['deadline'])
            now[0] += 10
            return AUTH_USER if path == '/auth/v1/user' else True

        def publication_search(query, fetch, document, tidy):
            fetch('https://publications.invalid/')
            return {'books': []}

        def fetch(url, deadline=None):
            seen.append(deadline)
            self.assertEqual(reader.remaining(deadline), 25)
            return '', url

        req = Request(self.base + '/api/publications', data=json.dumps({'query': NAME}).encode(),
                      headers={**HEADERS, 'Content-Type': 'application/json'})
        with patch.dict(os.environ, {'REQUIRE_AUTH': '1', 'READER_RPC_V2': '1'}), patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('cloud.remote', side_effect=remote), patch('publications.search', side_effect=publication_search), patch('reader.fetch_page', side_effect=fetch):
            with urlopen(req) as response:
                self.assertEqual(response.status, 200)
                response.read()
        self.assertEqual(seen, [145] * 3)

    def test_logout_timeout_clears_cookies_within_budget(self):
        entered, release = threading.Event(), threading.Event()
        workers = cloud.budget.BoundedWorkers(1, 'test-logout-dns')

        def open_url(request, deadline, connections):
            entered.set()
            release.wait(2)
            raise OSError('blocked test connection ended')

        req = Request(self.base + '/api/logout', data=b'{}', headers={**HEADERS, 'Content-Type': 'application/json'})
        with patch('reader.REQUEST_SECONDS', 0.05), patch('cloud.enabled', return_value=True), patch('cloud.config', return_value=('https://cloud.invalid', 'key')), patch.object(cloud.budget, 'NETWORK_WORKERS', workers), patch('cloud.budget.open_url', side_effect=open_url):
            try:
                start = time.monotonic()
                with urlopen(req) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
                    cookies = response.headers.get_all('Set-Cookie')
                    self.assertEqual(len(cookies), 2)
                    self.assertTrue(all('Max-Age=0' in cookie for cookie in cookies))
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.5)
            finally:
                release.set()
                self.assertTrue(workers.slots.acquire(timeout=1))
                workers.slots.release()

    def test_structured_logs_disabled_by_default(self):
        captured = io.StringIO()
        with redirect_stderr(captured), urlopen(self.base + '/api/health') as response:
            response.read()
        self.assertEqual(captured.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
