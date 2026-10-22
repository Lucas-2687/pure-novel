import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import cloud
import manage_user
import test_reader
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class CloudTests(unittest.TestCase):
    def test_username_normalization(self):
        self.assertEqual(cloud.username_email(' Reader_01 '), 'reader_01@users.pure-novel.invalid')
        for invalid in (None, 123, '', 'ab', '1reader', 'a' * 33, '读者', 'a b', 'a@b.com', 'admin\nother', 'a-b'):
            with self.subTest(username=invalid), self.assertRaises(cloud.CloudError):
                cloud.username_email(invalid)

    def test_username_password_login(self):
        outgoing = []
        session = {'access_token': 'access', 'refresh_token': 'refresh'}
        with patch('cloud.remote', return_value=session) as remote, patch('cloud.member', return_value={'id': 'uid', 'username': 'reader'}):
            result = cloud.login({'username': 'Reader', 'password': ' unchanged '}, outgoing)
        remote.assert_called_once_with('/auth/v1/token?grant_type=password', data={
            'email': 'reader@users.pure-novel.invalid', 'password': ' unchanged ',
        })
        self.assertEqual(result['user']['username'], 'reader')
        self.assertEqual(len(outgoing), 2)

    def test_invalid_login_does_not_call_auth(self):
        for data in ({'email': 'a@b.com', 'password': 'secret'}, {'username': 'reader', 'password': ''},
                     {'username': 'reader', 'password': None}, {'username': 'reader', 'password': 'x' * 257}):
            with self.subTest(data=data), patch('cloud.remote') as remote, self.assertRaises(cloud.CloudError):
                cloud.login(data, [])
            remote.assert_not_called()

    def test_failed_password_or_membership_sets_no_cookies(self):
        for failed_member in (False, True):
            outgoing = []
            with patch('cloud.remote', **({'return_value': {'access_token': 'access'}} if failed_member else {'side_effect': cloud.CloudError(401, '无效凭据')})), patch('cloud.member', side_effect=cloud.CloudError(403, '非成员')):
                with self.assertRaises(cloud.CloudError):
                    cloud.login({'username': 'reader', 'password': 'secret'}, outgoing)
            self.assertEqual(outgoing, [])

    def test_member_returns_username_not_internal_email(self):
        uid = '12345678-1234-1234-1234-123456789012'
        with patch('cloud.remote', side_effect=[{'id': uid, 'email': 'reader@users.pure-novel.invalid'}, [{'user_id': uid}]]):
            self.assertEqual(cloud.member('token'), {'id': uid, 'username': 'reader'})

    def test_create_username_account_and_membership(self):
        uid = '12345678-1234-1234-1234-123456789012'
        with patch('cloud.remote', side_effect=[{'id': uid}, {}]) as remote:
            result = manage_user.create_user('Reader', 'a-new-password')
        self.assertEqual(result, {'id': uid, 'username': 'reader'})
        self.assertEqual(remote.call_args_list[0].kwargs['data'], {
            'email': 'reader@users.pure-novel.invalid', 'password': 'a-new-password', 'email_confirm': True,
            'user_metadata': {'username': 'reader'},
        })
        self.assertEqual(remote.call_args_list[1].kwargs['data'], {'user_id': uid, 'active': True})
        self.assertTrue(all(call.kwargs['admin'] for call in remote.call_args_list))

    def test_create_account_rejects_short_password(self):
        with patch('cloud.remote') as remote, self.assertRaises(cloud.CloudError):
            manage_user.create_user('reader', 'short')
        remote.assert_not_called()

    def test_chinese_username_login_and_display(self):
        name = '测试读者'
        email = cloud.username_email(name)
        self.assertTrue(email.isascii())
        self.assertLessEqual(len(email.split('@')[0]), 64)
        self.assertEqual(email, cloud.username_email(' ' + name + ' '))
        self.assertNotEqual(email, cloud.username_email('我看看小说怎么了'))
        self.assertLessEqual(len(cloud.username_email('中' * 32).split('@')[0]), 64)
        uid = '12345678-1234-1234-1234-123456789012'
        with patch('cloud.remote', side_effect=[{'access_token': 'access', 'refresh_token': 'refresh'},
                   {'id': uid, 'email': email, 'user_metadata': {'username': name}}, [{'user_id': uid}]]) as remote:
            self.assertEqual(cloud.login({'username': name, 'password': 'test-password'}, [])['user']['username'], name)
        self.assertEqual(remote.call_args_list[0].kwargs['data']['email'], email)

    def test_mismatched_chinese_metadata_is_not_displayed(self):
        uid = '12345678-1234-1234-1234-123456789012'
        for metadata in ({'username': '其他用户名'}, {'username': '<script>'}, None):
            with patch('cloud.remote', side_effect=[{'id': uid, 'email': cloud.username_email('测试读者'), 'user_metadata': metadata}, [{}]]):
                self.assertEqual(cloud.member('token')['username'], '')

    def test_create_account_partial_failure_reports_repair(self):
        uid = '12345678-1234-1234-1234-123456789012'
        with patch('cloud.remote', side_effect=[{'id': uid}, cloud.CloudError(503, '失败')]):
            with self.assertRaises(cloud.CloudError) as error:
                manage_user.create_user('reader', 'a-new-password')
        self.assertIn(uid, str(error.exception))
        self.assertNotIn('a-new-password', str(error.exception))

    def test_fail_closed_on_vercel(self):
        with patch.dict(os.environ, {'VERCEL': '1'}, clear=True):
            self.assertTrue(cloud.required())
            with self.assertRaises(cloud.CloudError):
                cloud.config()

    def test_cookie_flags_and_logout(self):
        with patch.dict(os.environ, {'VERCEL': '1'}):
            headers = cloud.session_headers({'access_token': 'abc.def', 'refresh_token': 'refresh', 'expires_in': 3600})
            self.assertTrue(all('HttpOnly; SameSite=Strict' in h[1] and '; Secure' in h[1] for h in headers))
            self.assertTrue(all('Max-Age=0' in h[1] for h in cloud.session_headers()))

    def test_cookie_injection_rejected(self):
        with self.assertRaises(cloud.CloudError):
            cloud.session_headers({'access_token': 'bad\r\nheader', 'refresh_token': 'ok'})

    def test_inactive_member_denied(self):
        with patch('cloud.remote', side_effect=[{'id': '12345678-1234-1234-1234-123456789012'}, []]):
            with self.assertRaises(cloud.CloudError) as error:
                cloud.member('token')
            self.assertEqual(error.exception.status, 403)

    def test_sync_conflict(self):
        with patch('cloud.remote', return_value={'ok': False, 'version': 3}):
            with self.assertRaises(cloud.CloudError) as error:
                cloud.sync('token', {'version': 0, 'payload': {'books': [], 'settings': {}}})
            self.assertEqual(error.exception.status, 409)

    def test_refresh_membership_and_quota_share_budget(self):
        uid = '12345678-1234-1234-1234-123456789012'
        now, calls = [100.0], []

        def remote(path, **kwargs):
            calls.append((path, kwargs['deadline']))
            now[0] += 5
            if path.startswith('/auth/v1/token'):
                return {'access_token': 'new', 'refresh_token': 'new-refresh'}
            if path == '/auth/v1/user':
                return {'id': uid}
            if path.startswith('/rest/v1/members'):
                return [{'user_id': uid}]
            return True

        with patch('cloud.budget.time.monotonic', side_effect=lambda: now[0]), patch('cloud.remote', side_effect=remote):
            user, token = cloud.authenticate({'Cookie': 'pn_refresh=old'}, [], deadline=145)
            cloud.consume(user['id'], 'read', deadline=145)
        self.assertEqual(token, 'new')
        self.assertEqual([deadline for path, deadline in calls], [145] * 4)

    def test_expired_membership_never_starts_next_remote(self):
        now = [100.0]

        def remote(path, **kwargs):
            now[0] = 146
            return {'id': '12345678-1234-1234-1234-123456789012'}

        with patch('cloud.budget.time.monotonic', side_effect=lambda: now[0]), patch('cloud.remote', side_effect=remote) as call:
            with self.assertRaises(cloud.CloudError) as error:
                cloud.member('token', deadline=145)
            self.assertEqual(error.exception.status, 504)
            self.assertEqual(call.call_count, 1)

    def test_blocking_cloud_open_retains_network_capacity(self):
        entered, release = threading.Event(), threading.Event()
        workers = cloud.budget.BoundedWorkers(1, 'test-cloud-dns')

        def open_url(request, deadline, connections):
            entered.set()
            release.wait(2)
            raise OSError('test connection ended')

        with patch('cloud.config', return_value=('https://cloud.invalid', 'test-key')), patch.object(cloud.budget, 'NETWORK_WORKERS', workers), patch('cloud.budget.open_url', side_effect=open_url) as opener:
            try:
                start = time.monotonic()
                with self.assertRaises(cloud.CloudError) as error:
                    cloud.remote('/test', deadline=start + 0.06)
                self.assertEqual(error.exception.status, 504)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.5)
                with self.assertRaises(cloud.CloudError) as capacity:
                    cloud.remote('/test', deadline=time.monotonic() + 1)
                self.assertEqual(capacity.exception.status, 503)
                self.assertEqual(opener.call_count, 1)
            finally:
                release.set()
                self.assertTrue(workers.slots.acquire(timeout=1))
                workers.slots.release()

    def test_refresh_checks_membership(self):
        outgoing = []
        with patch('cloud.remote', return_value={'access_token': 'new', 'refresh_token': 'new-refresh'}), patch('cloud.member', return_value={'id': 'uid'}):
            user, token = cloud.authenticate({'Cookie': 'pn_refresh=old'}, outgoing)
        self.assertEqual(token, 'new')
        self.assertEqual(user['id'], 'uid')
        self.assertEqual(len(outgoing), 2)


class ProtectedHTTPTests(test_reader.HTTPTests):
    def test_openbooks_requires_member(self):
        for route, body in (('/api/openbooks', {'query': '西游记'}),
                            ('/api/openbooks/acquire', {'key': 'pg-23962', 'authorized': True})):
            req = Request(self.base + route, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            with patch.dict(os.environ, {'REQUIRE_AUTH': '1'}), patch('openbooks.acquire') as source:
                with self.assertRaises(HTTPError) as error:
                    urlopen(req)
                self.assertEqual(error.exception.code, 401)
                source.assert_not_called()

    def test_protected_fetch_never_calls_source(self):
        req = Request(self.base + '/api/read', data=b'{"url":"https://example.com/"}', headers={'Content-Type': 'application/json'})
        with patch.dict(os.environ, {'REQUIRE_AUTH': '1'}), patch('reader.load_reading') as source:
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 401)
            source.assert_not_called()

    def test_health_declares_auth(self):
        with patch.dict(os.environ, {'VERCEL': '1'}), urlopen(self.base + '/api/health') as response:
            self.assertTrue(json.load(response)['authRequired'])

    def test_state_authorized_and_versioned(self):
        req = Request(self.base + '/api/state', data=json.dumps({'version': 2, 'payload': {'books': [], 'settings': {}}}).encode(), headers={'Content-Type': 'application/json'})
        with patch.dict(os.environ, {'REQUIRE_AUTH': '1'}), patch('cloud.authenticate', return_value=({'id': 'uid'}, 'token')), patch('cloud.consume') as quota, patch('cloud.sync', return_value={'ok': True, 'version': 3}):
            with urlopen(req) as response:
                self.assertEqual(json.load(response)['version'], 3)
            quota.assert_called_once()
            self.assertEqual(quota.call_args.args, ('uid', 'sync'))
            self.assertGreater(quota.call_args.kwargs['deadline'], time.monotonic())