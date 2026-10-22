"""Deadline and single-flight regressions; all network traffic stays on loopback."""
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cloud
import reader
import request_budget as budget
from test_reader import BASE, HTTPTests, chapter


class BackendCase(unittest.TestCase):
    def setUp(self):
        with reader.CACHE_LOCK:
            reader.CACHE.clear()
            reader.SEARCH_CACHE.clear()
        for name, capacity in [('READING_WORKERS', 4), ('CHECK_WORKERS', 4),
                               ('SEARCH_WORKERS', 16), ('REDIRECT_WORKERS', 16)]:
            self.enterContext(patch.object(reader, name, budget.BoundedWorkers(capacity, 'test-' + name)))
        self.enterContext(patch.object(budget, 'NETWORK_WORKERS', budget.BoundedWorkers(16, 'test-network')))

    def assert_slots_released(self, workers, count):
        acquired = 0
        try:
            for _ in range(count):
                self.assertTrue(workers.slots.acquire(timeout=1))
                acquired += 1
        finally:
            for _ in range(acquired):
                workers.slots.release()


class DeadlineTests(BackendCase):
    def test_pagination_shares_default_budget_and_never_caches_partial(self):
        now, deadlines = [100.0], []

        def fetch(url, deadline=None):
            deadlines.append(deadline)
            now[0] += 10
            index = len(deadlines)
            return chapter(body=f'<p>第{index}页不同的测试正文。</p>' * 40,
                           nav=f'<a href="page{index + 1}.html">下一页</a>'), url

        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader.fetch_page', side_effect=fetch):
            result = reader.load_reading(BASE)
            self.assert_slots_released(reader.READING_WORKERS, 4)
        self.assertEqual(deadlines, [145.0] * 5)
        self.assertEqual(result['pages'], 4)
        self.assertIn('超时', ''.join(result['warnings']))
        self.assertNotIn(BASE, reader.CACHE)
        self.assertNotIn(BASE, reader.READING_INFLIGHT)

    def test_parse_expiry_prevents_new_pagination(self):
        now = [100.0]
        original = reader.parse_page

        def parse(html, url):
            result = original(html, url)
            now[0] = 146.0
            return result

        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader.parse_page', side_effect=parse), patch('reader.fetch_page', return_value=(chapter(nav='<a href="2.html">下一页</a>'), BASE)) as fetch:
            with self.assertRaises(reader.ReaderTimeout):
                reader.load_reading(BASE)
            self.assert_slots_released(reader.READING_WORKERS, 4)
        self.assertEqual(fetch.call_count, 1)
        self.assertNotIn(BASE, reader.CACHE)

    def test_intro_catalog_sample_use_one_deadline(self):
        now, calls = [100.0], []
        pages = ['<h1>测试小说</h1><a href="catalog/">查看目录</a>',
                 '<h1>测试小说</h1><a href="1.html">第一章 开始</a>', chapter()]

        def fetch(url, deadline=None):
            calls.append(deadline)
            now[0] += 16
            return pages[len(calls) - 1], url

        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader.fetch_page', side_effect=fetch):
            with self.assertRaises(reader.ReaderTimeout):
                reader.check_source('测试小说', BASE)
            self.assert_slots_released(reader.CHECK_WORKERS, 4)
        self.assertEqual(calls, [145.0] * 3)

    def test_sample_pagination_keeps_original_deadline(self):
        with patch('reader.fetch_page', return_value=(chapter(nav='<a href="2.html">下一页</a>'), BASE)), patch('reader.load_reading', return_value={'warnings': ['不完整']}) as load:
            deadline = time.monotonic() + 10
            with self.assertRaisesRegex(reader.ReaderError, '分页未完整'):
                reader.check_source('测试小说', BASE, deadline=deadline)
        self.assertEqual(load.call_args.kwargs['deadline'], deadline)

    def test_fetch_subcap_and_expired_redirect_do_not_restart(self):
        now = [100.0]
        seen = []

        def fetch(url, deadline, connections):
            seen.append(deadline)
            return '', url

        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader._fetch_page', side_effect=fetch):
            reader.fetch_page(BASE, deadline=145)
            reader.fetch_page(BASE, deadline=107)
        self.assertEqual(seen, [124.0, 107])

    def test_redirect_at_deadline_closes_response_without_starting_next_hop(self):
        now = [100.0]
        response = MagicMock()
        response.status = 302
        response.getheader.return_value = 'https://example.com/next/'

        def getresponse():
            now[0] = 124.0
            return response

        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader.public_addresses', return_value=['93.184.216.34']), patch('reader.PinnedConnection') as connection:
            connection.return_value.getresponse.side_effect = getresponse
            with self.assertRaises(reader.ReaderTimeout):
                reader.fetch_page(BASE, deadline=145)
            self.assert_slots_released(budget.NETWORK_WORKERS, 16)
        self.assertEqual(connection.call_count, 1)
        response.close.assert_called_once()

    def test_slow_dns_retains_capacity_and_never_connects_late(self):
        entered, release = threading.Event(), threading.Event()
        workers = budget.BoundedWorkers(1, 'blocked-dns')

        def dns(host, port):
            entered.set()
            release.wait(2)
            return ['93.184.216.34']

        with patch.object(budget, 'NETWORK_WORKERS', workers), patch('reader.public_addresses', side_effect=dns), patch('reader.PinnedConnection') as connection:
            try:
                start = time.monotonic()
                with self.assertRaises(reader.ReaderTimeout):
                    reader.fetch_page(BASE, deadline=start + 0.08)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.5)
                with self.assertRaisesRegex(reader.ReaderError, '其他请求'):
                    reader.fetch_page(BASE)
                connection.assert_not_called()
            finally:
                release.set()
                self.assert_slots_released(workers, 1)
            connection.assert_not_called()

    def test_slow_parser_retains_reading_placeholder(self):
        entered, release = threading.Event(), threading.Event()
        original = reader.parse_page

        def parse(html, url):
            entered.set()
            release.wait(2)
            return original(html, url)

        with patch('reader.fetch_page', return_value=(chapter(nav='<a href="2.html">下一页</a>'), BASE)) as fetch, patch('reader.parse_page', side_effect=parse):
            try:
                with self.assertRaises(reader.ReaderTimeout):
                    reader.load_reading(BASE, deadline=time.monotonic() + 0.08)
                self.assertTrue(entered.is_set())
                self.assertIn(BASE, reader.READING_INFLIGHT)
                with self.assertRaises(reader.ReaderTimeout):
                    reader.load_reading(BASE, deadline=time.monotonic() + 0.04)
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
                self.assert_slots_released(reader.READING_WORKERS, 4)
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn(BASE, reader.READING_INFLIGHT)
        with patch('reader.fetch_page', return_value=(chapter(), BASE)):
            self.assertEqual(reader.load_reading(BASE)['pages'], 1)


class SingleFlightTests(BackendCase):
    def test_normalized_urls_share_one_fetch_and_results_are_isolated(self):
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        original_wait = reader.await_result

        def fetch(url, deadline=None):
            entered.set()
            release.wait(2)
            return chapter(), url

        waits = []

        def wait_result(future, deadline):
            waits.append(future)
            if len(waits) == 2:
                joined.set()
            return original_wait(future, deadline)

        with ThreadPoolExecutor(max_workers=2) as callers, patch('reader.fetch_page', side_effect=fetch) as source, patch('reader.await_result', side_effect=wait_result):
            try:
                first = callers.submit(reader.load_reading, BASE)
                self.assertTrue(entered.wait(1))
                second = callers.submit(reader.load_reading, BASE.replace('example.com', 'EXAMPLE.COM:443') + '#section')
                self.assertTrue(joined.wait(1))
                self.assertIs(waits[0], waits[1])
            finally:
                release.set()
            one, two = first.result(1), second.result(1)
        self.assertEqual(source.call_count, 1)
        one['paragraphs'].append('mutated')
        self.assertNotIn('mutated', two['paragraphs'])
        self.assertNotIn('mutated', reader.load_reading(BASE)['paragraphs'])
        self.assertNotIn(BASE, reader.READING_INFLIGHT)

    def test_different_urls_are_not_serialized_under_cache_lock(self):
        barrier = threading.Barrier(2)

        def fetch(url, deadline=None):
            barrier.wait(1)
            return chapter(), url

        with ThreadPoolExecutor(max_workers=2) as callers, patch('reader.fetch_page', side_effect=fetch) as source:
            one = callers.submit(reader.load_reading, BASE)
            two = callers.submit(reader.load_reading, BASE + '?other=1')
            self.assertEqual(one.result(2)['pages'], 1)
            self.assertEqual(two.result(2)['pages'], 1)
        self.assertEqual(source.call_count, 2)

    def test_waiter_timeout_does_not_cancel_owner(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(url, deadline=None):
            entered.set()
            release.wait(2)
            return chapter(), url

        with ThreadPoolExecutor(max_workers=1) as callers, patch('reader.fetch_page', side_effect=fetch) as source:
            try:
                owner = callers.submit(reader.load_reading, BASE, time.monotonic() + 2)
                self.assertTrue(entered.wait(1))
                with self.assertRaises(reader.ReaderTimeout):
                    reader.load_reading(BASE, deadline=time.monotonic() + 0.05)
                self.assertIn(BASE, reader.READING_INFLIGHT)
                self.assertFalse(reader.READING_INFLIGHT[BASE]['future'].cancelled())
            finally:
                release.set()
            self.assertEqual(owner.result(1)['pages'], 1)
        self.assertEqual(source.call_count, 1)
        self.assertNotIn(BASE, reader.READING_INFLIGHT)

    def test_shared_failure_cleans_mapping_and_allows_retry(self):
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        original_wait = reader.await_result
        waits = []

        def fetch(url, deadline=None):
            entered.set()
            release.wait(2)
            raise reader.ReaderError('测试失败')

        def wait_result(future, deadline):
            waits.append(future)
            if len(waits) == 2:
                joined.set()
            return original_wait(future, deadline)

        with ThreadPoolExecutor(max_workers=2) as callers, patch('reader.fetch_page', side_effect=fetch) as source, patch('reader.await_result', side_effect=wait_result):
            try:
                one = callers.submit(reader.load_reading, BASE)
                self.assertTrue(entered.wait(1))
                two = callers.submit(reader.load_reading, BASE)
                self.assertTrue(joined.wait(1))
            finally:
                release.set()
            for future in (one, two):
                with self.assertRaisesRegex(reader.ReaderError, '测试失败'):
                    future.result(1)
        self.assertEqual(source.call_count, 1)
        self.assertNotIn(BASE, reader.READING_INFLIGHT)
        self.assertNotIn(BASE, reader.CACHE)
        with patch('reader.fetch_page', return_value=(chapter(), BASE)) as source:
            self.assertEqual(reader.load_reading(BASE)['pages'], 1)
        self.assertEqual(source.call_count, 1)

    def test_parse_failure_is_retryable_and_complete_cache_keeps_300_second_ttl(self):
        with patch('reader.fetch_page', return_value=('<h1>空网页</h1>', BASE)):
            with self.assertRaises(reader.ReaderError):
                reader.load_reading(BASE)
        self.assertNotIn(BASE, reader.READING_INFLIGHT)
        now = [100.0]
        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader.fetch_page', return_value=(chapter(), BASE)) as source:
            reader.load_reading(BASE)
            now[0] = 399.9
            reader.load_reading(BASE)
            self.assertEqual(source.call_count, 1)
            now[0] = 400.0
            reader.load_reading(BASE)
            self.assertEqual(source.call_count, 2)

    def test_uncancellable_tasks_have_no_unbounded_queue(self):
        entered, release = threading.Event(), threading.Event()
        workers = budget.BoundedWorkers(1, 'blocked-read')

        def fetch(url, deadline=None):
            entered.set()
            release.wait(2)
            return chapter(), url

        with patch.object(reader, 'READING_WORKERS', workers), patch('reader.fetch_page', side_effect=fetch) as source:
            try:
                with self.assertRaises(reader.ReaderTimeout):
                    reader.load_reading(BASE, deadline=time.monotonic() + 0.06)
                self.assertTrue(entered.is_set())
                for index in range(20):
                    url = BASE + '?other=' + str(index)
                    with self.assertRaisesRegex(reader.ReaderError, '其他请求'):
                        reader.load_reading(url)
                    self.assertNotIn(url, reader.READING_INFLIGHT)
                self.assertEqual(source.call_count, 1)
                self.assertIn(BASE, reader.READING_INFLIGHT)
            finally:
                release.set()
                self.assert_slots_released(workers, 1)
        self.assertNotIn(BASE, reader.READING_INFLIGHT)


class SearchDeadlineTests(BackendCase):
    html = '<h3><a href="https://example.com/book/">测试小说目录</a></h3>'

    def test_empty_and_failure_queries_are_retried_then_success_cached(self):
        for failure in (reader.ReaderError('网络失败'), ('<title>安全验证</title>', BASE), ('<title>空结果</title>', BASE)):
            with self.subTest(failure=failure):
                reader.SEARCH_CACHE.clear()
                kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch('reader.fetch_page', **kwargs):
                    self.assertFalse(reader.find_books('测试小说')['results'])
                self.assertFalse(reader.SEARCH_CACHE)
                with patch('reader.fetch_page', return_value=(self.html, BASE)) as fetch:
                    self.assertTrue(reader.find_books('测试小说')['results'])
                    self.assertTrue(reader.find_books(' 《测试小说》 ')['cached'])
                    self.assertEqual(fetch.call_count, 4)

    def test_success_ttl_and_capacity_remain_120_and_64(self):
        now = [100.0]
        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('reader.fetch_page', return_value=(self.html, BASE)) as fetch:
            reader.find_books('测试小说')
            now[0] = 219.9
            self.assertTrue(reader.find_books('测试小说')['cached'])
            self.assertEqual(fetch.call_count, 4)
            now[0] = 220.0
            self.assertNotIn('cached', reader.find_books('测试小说'))
            self.assertEqual(fetch.call_count, 8)
            reader.SEARCH_CACHE.clear()
            for index in range(64):
                reader.SEARCH_CACHE[(str(index), 0)] = (220, {})
            reader.find_books('测试小说')
            self.assertEqual(len(reader.SEARCH_CACHE), 64)
            self.assertNotIn(('0', 0), reader.SEARCH_CACHE)

    def test_slow_first_engine_preserves_later_completed_results(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(url, deadline=None):
            if 'bing.com' in url:
                entered.set()
                release.wait(2)
            return self.html, url

        with patch('reader.fetch_page', side_effect=fetch):
            try:
                start = time.monotonic()
                result = reader.find_books('测试小说', deadline=start + 0.12)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.6)
                self.assertEqual(result['results'][0]['engines'], ['360', '百度', '搜狗'])
                self.assertEqual(result['engines'][0]['state'], 'timeout')
                self.assertTrue(result['warnings'])
                self.assertTrue(reader.find_books('测试小说')['cached'])
            finally:
                release.set()
                self.assert_slots_released(reader.SEARCH_WORKERS, 16)
        self.assertEqual(reader.SEARCH_CACHE[('测试小说', 0)][1]['results'][0]['engines'], ['360', '百度', '搜狗'])

    def test_completed_redirect_survives_another_slow_redirect(self):
        release = threading.Event()
        html = self.html + '<h3><a href="/link?url=fast">测试小说</a></h3><h3><a href="/link?url=slow">测试小说</a></h3>'

        def fetch(url, deadline=None):
            if 'url=fast' in url:
                return '', 'https://example.com/fast/'
            if 'url=slow' in url:
                release.wait(2)
                return '', 'https://example.com/slow/'
            return html, 'https://www.sogou.com/'

        with patch('reader.SEARCH_ENGINES', {'搜狗': 'https://www.sogou.com/web?query='}), patch('reader.fetch_page', side_effect=fetch):
            try:
                result = reader.find_books('测试小说', deadline=time.monotonic() + 0.12)
                self.assertEqual({entry['url'] for entry in result['results']}, {'https://example.com/book/', 'https://example.com/fast/'})
                self.assertTrue(result['warnings'])
            finally:
                release.set()
                self.assert_slots_released(reader.REDIRECT_WORKERS, 16)
                self.assert_slots_released(reader.SEARCH_WORKERS, 16)

    def test_late_search_page_never_starts_redirect(self):
        release = threading.Event()

        def fetch(url, deadline=None):
            release.wait(2)
            return '<h3><a href="/link?url=x">测试小说</a></h3>', 'https://www.sogou.com/'

        with patch('reader.fetch_page', side_effect=fetch) as fetch:
            try:
                result = reader.find_books('测试小说', deadline=time.monotonic() + 0.06)
                self.assertFalse(result['results'])
                self.assertFalse(reader.SEARCH_CACHE)
            finally:
                release.set()
                self.assert_slots_released(reader.SEARCH_WORKERS, 16)
            self.assertEqual(fetch.call_count, 4)


class LocalTransportTests(BackendCase):
    @classmethod
    def setUpClass(cls):
        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    if self.path == '/json':
                        data = b'{"ok":true}'
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if self.path == '/headers':
                        self.connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                        for _ in range(40):
                            self.connection.sendall(b'a')
                            time.sleep(0.02)
                        return
                    if self.path == '/redirect':
                        self.send_response(302)
                        self.send_header('Location', '/body')
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', '500')
                    self.end_headers()
                    for _ in range(50):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
        cls.thread = threading.Thread(target=lambda: cls.server.serve_forever(poll_interval=0.01), daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(1)

    def test_real_slow_headers_body_and_redirect_are_bounded(self):
        original = reader.PinnedConnection

        def connection(host, ip, secure):
            conn = original(host, ip, False)
            conn.port = self.server.server_port
            return conn

        with patch('reader.public_addresses', return_value=['127.0.0.1']), patch('reader.PinnedConnection', side_effect=connection):
            for path in ('/headers', '/body', '/redirect'):
                with self.subTest(path=path):
                    start = time.monotonic()
                    with self.assertRaises(reader.ReaderTimeout):
                        reader.fetch_page('http://example.com' + path, deadline=start + 0.12)
                    self.assertLess(time.monotonic() - start, 0.6)
                    self.assert_slots_released(budget.NETWORK_WORKERS, 16)

    def test_cloud_successful_json_uses_deadline_transport(self):
        with patch('cloud.config', return_value=(f'http://127.0.0.1:{self.server.server_port}', 'test-key')):
            self.assertEqual(cloud.remote('/json', deadline=time.monotonic() + 1), {'ok': True})
        self.assert_slots_released(budget.NETWORK_WORKERS, 16)

    def test_cloud_real_slow_response_is_bounded(self):
        with patch('cloud.config', return_value=(f'http://127.0.0.1:{self.server.server_port}', 'test-key')):
            start = time.monotonic()
            with self.assertRaises(cloud.CloudError) as error:
                cloud.remote('/body', deadline=start + 0.12)
            self.assertEqual(error.exception.status, 504)
            self.assertLess(time.monotonic() - start, 0.6)
            self.assert_slots_released(budget.NETWORK_WORKERS, 16)


class BudgetHTTPTests(HTTPTests):
    def test_http_deadline_returns_partial_warning_without_late_pagination(self):
        release = threading.Event()
        calls = []

        def fetch(url, deadline=None):
            calls.append(url)
            if len(calls) == 1:
                return chapter(nav='<a href="2.html">下一页</a>'), url
            release.wait(2)
            return chapter(nav='<a href="3.html">下一页</a>'), url

        reader.CACHE.clear()
        req = Request(self.base + '/api/read', data=json.dumps({'url': BASE}).encode(), headers={'Content-Type': 'application/json'})
        with patch('reader.REQUEST_SECONDS', 0.1), patch('reader.fetch_page', side_effect=fetch):
            task = None
            try:
                start = time.monotonic()
                with urlopen(req, timeout=1) as response:
                    result = json.load(response)
                self.assertLess(time.monotonic() - start, 0.6)
                self.assertEqual(result['pages'], 1)
                self.assertIn('超时', ''.join(result['warnings']))
                self.assertNotIn(BASE, reader.CACHE)
                self.assertIn(BASE, reader.READING_INFLIGHT)
                task = reader.READING_INFLIGHT[BASE]['future']
            finally:
                release.set()
                if task is not None:
                    task.result(1)
            self.assertEqual(len(calls), 2)
        self.assertNotIn(BASE, reader.READING_INFLIGHT)
        self.assertNotIn(BASE, reader.CACHE)

    def test_concurrent_http_readers_each_pass_auth_and_quota(self):
        release, entered, joined = threading.Event(), threading.Event(), threading.Event()
        original = reader.await_result
        waits = []
        reader.CACHE.clear()

        def fetch(url, deadline=None):
            entered.set()
            release.wait(2)
            return chapter(), url

        def wait_result(future, deadline):
            waits.append(future)
            if len(waits) == 2:
                joined.set()
            return original(future, deadline)

        def request():
            req = Request(self.base + '/api/read', data=json.dumps({'url': BASE}).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=2) as response:
                return json.load(response)

        with ThreadPoolExecutor(max_workers=2) as callers, patch('cloud.required', return_value=True), patch('cloud.authenticate', return_value=({'id': 'uid'}, 'token')) as auth, patch('cloud.consume') as quota, patch('reader.fetch_page', side_effect=fetch) as source, patch('reader.await_result', side_effect=wait_result):
            try:
                one = callers.submit(request)
                self.assertTrue(entered.wait(1))
                two = callers.submit(request)
                self.assertTrue(joined.wait(1))
            finally:
                release.set()
            self.assertEqual(one.result(1)['pages'], 1)
            self.assertEqual(two.result(1)['pages'], 1)
            self.assertEqual(source.call_count, 1)
            self.assertEqual(auth.call_count, 2)
            self.assertEqual(quota.call_count, 2)

    def test_auth_quota_and_source_share_deadline(self):
        for route, function, body in (('/api/read', 'load_reading', {'url': BASE}),
                                      ('/api/search', 'find_books', {'query': '测试小说'}),
                                      ('/api/check', 'check_source', {'query': '测试小说', 'url': BASE})):
            now, seen = [100.0], []

            def auth(headers, outgoing, deadline=None):
                seen.append(deadline)
                now[0] += 10
                return {'id': 'uid'}, 'token'

            def consume(uid, route, deadline=None):
                seen.append(deadline)
                now[0] += 10

            def source(*args, deadline=None):
                seen.append(deadline)
                self.assertEqual(reader.remaining(deadline), 25)
                return {'ok': True}

            req = Request(self.base + route, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('cloud.required', return_value=True), patch('cloud.authenticate', side_effect=auth), patch('cloud.consume', side_effect=consume), patch('reader.' + function, side_effect=source), urlopen(req) as response:
                self.assertTrue(json.load(response)['ok'])
            self.assertEqual(seen, [145.0] * 3)

    def test_auth_budget_exhaustion_never_calls_quota_or_cached_source(self):
        now = [100.0]

        def auth(headers, outgoing, deadline=None):
            now[0] = 146
            return {'id': 'uid'}, 'token'

        req = Request(self.base + '/api/read', data=json.dumps({'url': BASE}).encode(), headers={'Content-Type': 'application/json'})
        with patch('reader.time.monotonic', side_effect=lambda: now[0]), patch('cloud.required', return_value=True), patch('cloud.authenticate', side_effect=auth), patch('cloud.consume') as quota, patch('reader.load_reading') as source:
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 504)
            self.assertEqual(json.load(error.exception)['category'], 'timeout')
            quota.assert_not_called()
            source.assert_not_called()

    def test_each_cached_read_still_authenticates_and_consumes_quota(self):
        reader.CACHE.clear()
        req = Request(self.base + '/api/read', data=json.dumps({'url': BASE}).encode(), headers={'Content-Type': 'application/json'})
        with patch('cloud.required', return_value=True), patch('cloud.authenticate', return_value=({'id': 'uid'}, 'token')) as auth, patch('cloud.consume') as quota, patch('reader.fetch_page', return_value=(chapter(), BASE)) as fetch:
            for _ in range(2):
                with urlopen(req) as response:
                    self.assertEqual(json.load(response)['kind'], 'chapter')
            self.assertEqual(auth.call_count, 2)
            self.assertEqual(quota.call_count, 2)
            self.assertEqual(fetch.call_count, 1)

    def test_input_validation_precedes_authentication(self):
        req = Request(self.base + '/api/search', data=json.dumps({'query': '测试小说', 'round': True}).encode(), headers={'Content-Type': 'application/json'})
        with patch('cloud.required', return_value=True), patch('cloud.authenticate') as auth:
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 422)
            auth.assert_not_called()


if __name__ == '__main__':
    unittest.main()
