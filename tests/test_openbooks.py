import unittest
from unittest.mock import patch

import openbooks


class OpenBooksTests(unittest.TestCase):
    def raw(self):
        return ('Project Gutenberg [EBook #23962]\n第一回 开始\n正文测试。\n第二回 继续\n正文二。\n'
                '*** END OF THE PROJECT GUTENBERG EBOOK ***\nLicense retained').encode('utf-8')

    def test_search_aliases(self):
        self.assertEqual(openbooks.search('西游记')['books'][0]['key'], 'pg-23962')
        self.assertEqual(openbooks.search('西遊記')['books'][0]['key'], 'pg-23962')
        self.assertEqual(openbooks.search('不存在的小说')['books'], [])
        for invalid in (None, [], '', 'a' * 61):
            with self.assertRaises(ValueError):
                openbooks.search(invalid)

    def test_sections_and_license_retained(self):
        data = openbooks.prepare(openbooks.CATALOG[0], self.raw())
        self.assertEqual(len(data['id']), 64)
        self.assertEqual(len(data['sections']), 3)
        self.assertEqual(data['sections'][1]['title'], '第一回 开始')
        self.assertIn('License retained', data['sections'][-1]['paragraphs'])

    def test_bad_files(self):
        for raw in (b'', b'<html>login</html>', self.raw().replace(b'23962', b'12345'),
                    self.raw().split(b'*** END')[0], b'\xff\xfe', self.raw() + b'\x00',
                    b'x' * (openbooks.MAX_BYTES + 1)):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                openbooks.prepare(openbooks.CATALOG[0], raw)

    def test_circle_zero_chapter_titles(self):
        raw = self.raw().replace('第二回'.encode(), '第一○○回'.encode())
        data = openbooks.prepare(openbooks.CATALOG[0], raw)
        self.assertEqual(len(data['sections']), 3)
        self.assertEqual(data['sections'][2]['title'], '第一○○回 继续')

    def test_no_arbitrary_url(self):
        with self.assertRaises(ValueError):
            openbooks.acquire('https://127.0.0.1/private', None, None)
        with self.assertRaises(ValueError):
            openbooks.fetch_text('https://example.com/file.txt', None, None)

    def test_acquire_uses_reviewed_url(self):
        with patch('openbooks.fetch_text', return_value=self.raw()) as fetch:
            data = openbooks.acquire('pg-23962', None, None)
            self.assertEqual(data['title'], '西遊記')
            self.assertEqual(fetch.call_args.args[0],
                             'https://mirror.cs.odu.edu/gutenberg/2/3/9/6/23962/23962-0.txt')

    def test_network_checks(self):
        class Response:
            status = 200
            headers = {'Content-Type': 'text/plain', 'Content-Length': '3'}

            def getheader(self, key, default=None):
                return self.headers.get(key, default)

            def read1(self, size):
                return b''

        class Connection:
            closed = False

            def __init__(self, *args):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return response

            def close(self):
                self.closed = True

        response = Response()
        for status, headers in ((302, {'Location': 'https://127.0.0.1/'}),
                                (200, {'Content-Type': 'text/html'}),
                                (200, {'Content-Type': 'text/plain', 'Content-Length': '3'}),
                                (200, {'Content-Type': 'text/plain', 'Content-Length': str(openbooks.MAX_BYTES + 1)})):
            response.status, response.headers = status, headers
            with self.subTest(status=status, headers=headers), self.assertRaises(ValueError):
                openbooks.fetch_text(openbooks.file_url(openbooks.CATALOG[0]), lambda *args: ['8.8.8.8'], Connection)


if __name__ == '__main__':
    unittest.main()