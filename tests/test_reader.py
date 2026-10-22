import json
import gzip
import base64
from email.message import Message
from unittest.mock import MagicMock
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reader

BASE = 'https://example.com/shu/1/10.html'


def chapter(title='第一章 开始', body=None, nav=''):
    body = body if body is not None else '<p>这是用于测试的正文，不应删除。</p>' * 30
    return f'<html><title>{title}_测试小说_阅读站</title><h1>{title}</h1><div id="content">{body}</div>{nav}</html>'


class ParsingTests(unittest.TestCase):
    def setUp(self):
        reader.CACHE.clear()

    def test_extract_text_without_scripts_ads_or_recommendation(self):
        html = chapter(body='<script>alert(1)</script><div class="ad">垃圾广告</div><a href="/ad">推广</a>' + '<p>正文内容，安全渲染。</p>' * 30,
                       nav='<a href="/shu/1/">章节列表</a><a href="11.html">下一章</a>')
        result = reader.parse_page(html, BASE)
        self.assertEqual(result['kind'], 'chapter')
        self.assertEqual(result['bookTitle'], '测试小说')
        self.assertEqual(result['next'], 'https://example.com/shu/1/11.html')
        text = ''.join(result['paragraphs'])
        self.assertNotIn('alert', text)
        self.assertNotIn('垃圾广告', text)
        self.assertNotIn('推广', text)

    def test_empty_chapter_must_not_use_recommended_book(self):
        with self.assertRaises(reader.ReaderError):
            reader.parse_page(chapter(body='') + '<aside>' + '其他小说正文' * 1000 + '</aside>', BASE)

    def test_private_font_characters_rejected(self):
        with self.assertRaises(reader.ReaderError):
            reader.parse_page(chapter(body='正常\ue001\ue002' * 100), BASE)

    def test_catalog_and_pagination(self):
        html = '<h1>测试小说</h1><a href="1.html">第一章 开始</a><a href="2.html">第二章 继续</a><a href="page2/">下一页</a>'
        result = reader.parse_page(html, BASE)
        self.assertEqual(result['kind'], 'catalog')
        self.assertEqual(len(result['chapters']), 2)
        self.assertEqual(result['catalogNext'], 'https://example.com/shu/1/page2/')
        self.assertIsNone(result['nextPage'])

    def test_latest_block_does_not_break_catalog_order(self):
        html = '<h1>测试小说</h1><a href="3.html">第三章</a><a href="2.html">第二章</a>'
        html += '<a href="1.html">第一章</a><a href="2.html">第二章</a><a href="3.html">第三章</a>'
        result = reader.parse_page(html, BASE)
        self.assertEqual([c['title'] for c in result['chapters']], ['第一章', '第二章', '第三章'])

    def test_multi_page_chapter_merge(self):
        first = chapter(nav='<a href="10_2.html">下一页</a>')
        second = chapter(body='<p>第二页继续讲述不同的内容。</p>' * 30, nav='<a href="11.html">下一章</a>')
        with patch('reader.fetch_page', side_effect=[(first, BASE), (second, BASE.replace('.html', '_2.html'))]):
            result = reader.load_reading(BASE)
        self.assertEqual(result['pages'], 2)
        self.assertEqual(len(result['paragraphs']), 60)
        self.assertFalse(result['warnings'])
        self.assertTrue(result['next'].endswith('/11.html'))

    def test_wrong_title_page_returns_visible_warning_not_silent_merge(self):
        first = chapter(nav='<a href="10_2.html">下一页</a>')
        second = chapter(title='第二章 错误章节')
        with patch('reader.fetch_page', side_effect=[(first, BASE), (second, BASE.replace('.html', '_2.html'))]):
            result = reader.load_reading(BASE)
        self.assertEqual(result['pages'], 1)
        self.assertTrue(result['warnings'])
        self.assertNotIn(BASE, reader.CACHE)

    def test_duplicate_page_detected(self):
        first = chapter(nav='<a href="10_2.html">下一页</a>')
        with patch('reader.fetch_page', side_effect=[(first, BASE), (first, BASE.replace('.html', '_2.html'))]):
            result = reader.load_reading(BASE)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['pages'], 1)

    def test_homepage_not_mistaken_for_chapter(self):
        with self.assertRaises(reader.ReaderError):
            reader.parse_page('<h1>小说网</h1><div>' + '推荐简介' * 1000 + '</div>', BASE)


class SecurityTests(unittest.TestCase):
    def test_bounded_gzip(self):
        self.assertEqual(reader.decompress_bounded(gzip.compress(b'hello'), 'gzip'), b'hello')
        with patch('reader.MAX_BYTES', 100), self.assertRaises(reader.ReaderError):
            reader.decompress_bounded(gzip.compress(b'x' * 10000), 'gzip')

    def test_bad_urls(self):
        for url in ('file:///etc/passwd', 'https://user:pass@example.com/', 'http://example.com:8080/',
                    'https://localhost/', 'http://server.local/', 'https://example.com\\@127.0.0.1/', 'https://example.com/\r\nHost:x'):
            with self.subTest(url=url), self.assertRaises(reader.ReaderError):
                reader.validate_url(url)

    def test_private_dns_results_rejected(self):
        for ip in ('127.0.0.1', '10.0.0.1', '192.168.1.2', '169.254.169.254', '::1', '::ffff:127.0.0.1', '0.0.0.0'):
            with self.subTest(ip=ip), patch('reader.socket.getaddrinfo', return_value=[(2, 1, 6, '', (ip, 443))]):
                with self.assertRaises(reader.ReaderError):
                    reader.public_addresses('example.com', 443)

    def test_no_active_html_in_json_paragraphs(self):
        result = reader.parse_page(chapter(body='<img src=x onerror=alert(1)>' + '<p>安全文本测试。</p>' * 40), BASE)
        self.assertNotIn('<img', ''.join(result['paragraphs']))


class SearchTests(unittest.TestCase):
    def setUp(self):
        reader.SEARCH_CACHE.clear()

    def test_search_round_validation(self):
        for value in (-1, 3, '1', True):
            with self.subTest(value=value), self.assertRaises(reader.ReaderError):
                reader.find_books('测试小说', value)

    def test_baidu_direct_destination_in_container(self):
        html = '<div mu="https://example.com/book/"><h3><a href="https://www.baidu.com/link?url=x">测试小说目录</a></h3></div>'
        result = reader.search_results(html, 'https://www.baidu.com/', '测试小说', '百度')
        self.assertEqual(result[0]['url'], 'https://example.com/book/')

    def test_search_resolves_matching_engine_redirect(self):
        html = '<h3><a href="/link?url=abc">测试小说目录</a></h3>'
        def fetch(url, deadline=None):
            if '/link?' in url:
                return '<title>测试小说</title>', 'https://example.com/book/'
            return html, 'https://www.sogou.com/web'
        with patch('reader.fetch_page', side_effect=fetch):
            result = reader.find_books('测试小说')
        self.assertTrue(result['results'])
        self.assertTrue(all(e['host'] == 'example.com' for e in result['results']))

    def test_failed_engine_redirect_not_reported_as_source(self):
        def fetch(url, deadline=None):
            if '/link?' in url:
                raise reader.ReaderError('已阻止从 HTTPS 降级到 HTTP 的跳转。')
            return '<h3><a href="/link?url=x">测试小说</a></h3>', 'https://www.sogou.com/'
        with patch('reader.fetch_page', side_effect=fetch):
            self.assertFalse(reader.find_books('测试小说')['results'])

    def test_cookie_redirect_session_and_cross_host_isolation(self):
        responses = []
        for status, location in [(302, BASE), (302, 'https://other.example/book/'), (200, None)]:
            response = MagicMock()
            response.status = status
            headers = Message()
            headers['Content-Type'] = 'text/html; charset=utf-8'
            if location:
                headers['Location'] = location
            if not responses:
                headers['Set-Cookie'] = 'session=abc; Path=/; Secure'
            response.info.return_value = headers
            response.getheader.side_effect = lambda key, default=None, h=headers: h.get(key, default)
            response.read1.return_value = b''
            responses.append(response)
        with patch('reader.public_addresses', return_value=['93.184.216.34']), patch('reader.PinnedConnection') as connection:
            connection.return_value.getresponse.side_effect = responses
            reader.fetch_page(BASE)
        calls = connection.return_value.request.call_args_list
        self.assertNotIn('Cookie', calls[0].kwargs['headers'])
        self.assertEqual(calls[1].kwargs['headers']['Cookie'], 'session=abc')
        self.assertNotIn('Cookie', calls[2].kwargs['headers'])

    def test_redirect_cycle_stops_early(self):
        response = MagicMock()
        response.status = 302
        response.info.return_value = Message()
        response.getheader.return_value = BASE
        with patch('reader.public_addresses', return_value=['93.184.216.34']), patch('reader.PinnedConnection') as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(reader.ReaderError, '跳转循环'):
                reader.fetch_page(BASE)
            self.assertEqual(connection.call_count, 2)

    def test_https_downgrade_still_blocked(self):
        response = MagicMock()
        response.status = 302
        response.info.return_value = Message()
        response.getheader.return_value = 'http://example.com/book/'
        with patch('reader.public_addresses', return_value=['93.184.216.34']), patch('reader.PinnedConnection') as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(reader.ReaderError, '降级'):
                reader.fetch_page(BASE)

    def test_challenge_and_empty_page_not_success(self):
        with patch('reader.fetch_page', return_value=('<title>安全验证</title>', 'https://www.baidu.com/')):
            result = reader.find_books('测试小说')
        self.assertTrue(all(e['state'] == 'blocked' for e in result['engines']))
        reader.SEARCH_CACHE.clear()
        with patch('reader.fetch_page', return_value=('<title>搜索结果</title>', 'https://www.baidu.com/')):
            result = reader.find_books('测试小说')
        self.assertTrue(all(e['state'] == 'empty_or_unparsed' for e in result['engines']))

    def test_supplement_changes_query_and_cache_avoids_repeat(self):
        html = '<h3><a href="https://example.com/book/">测试小说目录</a></h3>'
        with patch('reader.fetch_page', return_value=(html, 'https://www.so.com/')) as fetch:
            reader.find_books('测试小说', 1)
            self.assertTrue(reader.find_books('测试小说', 1)['cached'])
            self.assertEqual(fetch.call_count, 4)
            self.assertTrue(all('%E6%9C%80%E6%96%B0' in call.args[0] for call in fetch.call_args_list))
    def test_non_reading_search_results_filtered(self):
        urls = ['https://ai.so.com/search/a', 'https://wenda.so.com/q/1',
                'https://baike.so.com/doc/1', 'https://tieba.baidu.com/p/1',
                'https://www.xxsy.net/ask/abc', 'https://example.com/keyword/test.shtml']
        html = ''.join(f'<h3><a href="{u}">测试小说目录</a></h3>' for u in urls)
        self.assertEqual(reader.search_results(html, 'https://www.so.com/', '测试小说', '360'), [])

    def test_numeric_descending_catalog_reversed(self):
        html = '<h1>测试小说</h1>' + ''.join(f'<a href="{i}.html">第{i}章 测试</a>' for i in (3, 2, 1))
        result = reader.parse_page(html, BASE)
        self.assertEqual([c['title'] for c in result['chapters']], ['第1章 测试', '第2章 测试', '第3章 测试'])

    def test_qq_book_title_from_explicit_book_link(self):
        html = chapter() + '<a class="book-title ypc-link" href="//book.qq.com/book-detail/123">实际书名</a>'
        result = reader.parse_page(html, 'https://book.qq.com/book-read/123/1')
        self.assertEqual(result['bookTitle'], '实际书名')

    def test_query_validation(self):
        self.assertEqual(reader.search_term(' 《测试小说》 '), '测试小说')
        for text in ('', 'a', '！！！', 'x' * 61, '书名\n伪造', None):
            with self.subTest(text=text), self.assertRaises(reader.ReaderError):
                reader.search_term(text)

    def test_search_match_dedup_and_tracking_decode(self):
        target = 'https://example.com/book/'
        payload = base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
        html = f'<h2><a href="https://www.bing.com/ck/a?u=a1{payload}">测试小说最新章节</a></h2>'
        html += '<h2><a href="https://example.com/book/">测试小说目录</a></h2>'
        html += '<h2><a href="https://example.com/game/">其他游戏</a></h2>'
        html += '<h2><a href="javascript:alert(1)">测试小说</a></h2>'
        result = reader.search_results(html, 'https://www.bing.com/search', '测试小说', 'Bing')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['url'], target)

    def test_360_direct_destination(self):
        html = '<h3><a href="https://www.so.com/link?m=abc" data-mdurl="https://example.com/book/">测试小说目录</a></h3>'
        result = reader.search_results(html, 'https://www.so.com/s', '测试小说', '360')
        self.assertEqual(result[0]['url'], 'https://example.com/book/')

    def test_partial_search_failure_and_engine_merge(self):
        html = '<h2><a href="https://example.com/book/">测试小说目录</a></h2>'
        with patch('reader.fetch_page', return_value=(html, 'https://www.bing.com/')):
            result = reader.find_books('测试小说')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['engines'], ['Bing', '360', '百度', '搜狗'])
        reader.SEARCH_CACHE.clear()
        with patch('reader.fetch_page', side_effect=reader.ReaderError('搜索连接失败')):
            result = reader.find_books('测试小说')
        self.assertEqual(result['results'], [])
        self.assertEqual(len(result['warnings']), 4)

    def test_verify_catalog_sample(self):
        catalog = '<h1>测试小说</h1><a href="1.html">第一章 开始</a>'
        with patch('reader.fetch_page', side_effect=[(catalog, BASE), (chapter(), BASE)]):
            result = reader.check_source('测试小说', BASE)
        self.assertEqual(result['chapters'], 1)
        self.assertGreater(result['characters'], 180)

    def test_encyclopedia_filtered(self):
        html = '<h3><a href="https://baike.sogou.com/v1">测试小说</a></h3>'
        self.assertEqual(reader.search_results(html, 'https://www.sogou.com/', '测试小说', '搜狗'), [])

    def test_challenge_200_not_reported_as_parse_failure(self):
        with self.assertRaisesRegex(reader.ReaderError, '人机验证'):
            reader.parse_page('<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/a"></script>', BASE)

    def test_introduction_follows_explicit_same_site_catalog(self):
        intro = '<h1>测试小说</h1><a href="catalog/">查看目录</a><a href="https://other.example/">立即阅读</a>'
        catalog = '<h1>测试小说</h1><a href="1.html">第一章 开始</a>'
        with patch('reader.fetch_page', side_effect=[(intro, BASE), (catalog, 'https://example.com/catalog/'), (chapter(), 'https://example.com/catalog/1.html')]) as fetch:
            result = reader.check_source('测试小说', BASE)
        self.assertEqual(result['kind'], 'catalog')
        self.assertEqual(fetch.call_count, 3)

    def test_incomplete_pagination_not_verified(self):
        html = chapter(nav='<a href="next.html">下一页</a>')
        with patch('reader.fetch_page', return_value=(html, BASE)), patch('reader.load_reading', return_value={'warnings': ['分页错误']}):
            with self.assertRaisesRegex(reader.ReaderError, '分页未完整'):
                reader.check_source('测试小说', BASE)

    def test_directory_sample_chapter_title_must_match(self):
        catalog = '<h1>测试小说</h1><a href="1.html">第一章 山边小村</a>'
        with patch('reader.fetch_page', side_effect=[(catalog, BASE), (chapter(title='第一章 其他内容'), BASE)]):
            with self.assertRaisesRegex(reader.ReaderError, '章节标题不匹配'):
                reader.check_source('测试小说', BASE)

    def test_wrong_book_and_cross_site_sample_rejected(self):
        with patch('reader.fetch_page', return_value=(chapter(), BASE)), self.assertRaises(reader.ReaderError):
            reader.check_source('另外一本书', BASE)
        catalog = '<h1>测试小说</h1><a href="1.html">第一章 开始</a>'
        with patch('reader.fetch_page', side_effect=[(catalog, BASE), (chapter(), 'https://other.example/book/')]), self.assertRaises(reader.ReaderError):
            reader.check_source('测试小说', BASE)

    def test_catalog_notices_not_used_as_readable_chapter(self):
        catalog = '<h1>测试小说</h1><a href="1.html">第一章 新书公告</a>'
        with patch('reader.fetch_page', return_value=(catalog, BASE)), self.assertRaises(reader.ReaderError):
            reader.check_source('测试小说', BASE)


class HTTPTests(unittest.TestCase):
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

    def test_all_frontend_assets(self):
        for path in ('/', '/app.js', '/ebooks.js', '/storage.js', '/style.css', '/sw.js', '/manifest.webmanifest', '/icon.svg'):
            with self.subTest(path=path), urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 20)
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_api_mock_success(self):
        req = Request(self.base + '/api/read', data=json.dumps({'url': BASE}).encode(), headers={'Content-Type': 'application/json'})
        with patch('reader.load_reading', return_value={'kind': 'chapter'}), urlopen(req) as response:
            self.assertEqual(json.load(response)['kind'], 'chapter')

    def test_search_and_check_routes(self):
        for route, function, body in (('/api/search', 'find_books', {'query': '测试小说'}),
                                      ('/api/check', 'check_source', {'query': '测试小说', 'url': BASE})):
            req = Request(self.base + route, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            with self.subTest(route=route), patch('reader.' + function, return_value={'ok': True}), urlopen(req) as response:
                self.assertTrue(json.load(response)['ok'])

    def test_cross_origin_rejected(self):
        req = Request(self.base + '/api/read', data=b'{"url":"https://example.com/"}',
                      headers={'Content-Type': 'application/json', 'Origin': 'https://evil.example'})
        with self.assertRaises(HTTPError) as err:
            urlopen(req)
        self.assertEqual(err.exception.code, 403)

    def test_openbook_search_and_acquire_guard(self):
        req = Request(self.base + '/api/openbooks', data=json.dumps({'query': '西游记'}).encode(),
                      headers={'Content-Type': 'application/json'})
        with urlopen(req) as response:
            self.assertEqual(json.load(response)['books'][0]['key'], 'pg-23962')
        for body in ({'key': 'pg-23962'}, {'key': 'https://127.0.0.1/file', 'authorized': True}):
            req = Request(self.base + '/api/openbooks/acquire', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json'})
            with patch('openbooks.fetch_text') as fetch, self.assertRaises(HTTPError) as err:
                urlopen(req)
            self.assertEqual(err.exception.code, 422)
            fetch.assert_not_called()

    def test_invalid_input(self):
        req = Request(self.base + '/api/read', data=b'[]', headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as err:
            urlopen(req)
        self.assertEqual(err.exception.code, 400)


if __name__ == '__main__':
    unittest.main()