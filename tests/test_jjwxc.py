import unittest
from unittest.mock import patch
import reader

BASE = 'https://wap.jjwxc.net/book2/6407777'
TITLE = '<title>《女儿的火葬场》山茶猫 ^第1章^ 晋江文学城</title>'


class JinjiangTests(unittest.TestCase):
    def setUp(self):
        reader.CACHE.clear()

    def test_expand_catalog_only_on_official_host(self):
        self.assertEqual(reader.reading_url(BASE), BASE + '?more=0&whole=1')
        self.assertEqual(reader.reading_url(BASE + '/1'), BASE + '/1')
        self.assertEqual(reader.reading_url('https://evil.example/book2/1'), 'https://evil.example/book2/1')

    def test_catalog_identifies_locked_chapters_and_ignores_teaser(self):
        html = TITLE + '<a href="/book2/6407777/1"><span>1.</span> 梦的开始&nbsp;&nbsp;简介</a><a href="/vip/6407777/24?ctime=123">24. 致命一击&nbsp;简介</a>'
        r = reader.parse_page(html, BASE)
        self.assertEqual(r['author'], '山茶猫')
        self.assertEqual(r['chapters'][0]['title'], '第1章 梦的开始')
        self.assertFalse(r['chapters'][0]['locked'])
        self.assertTrue(r['chapters'][1]['locked'])
        self.assertIn('VIP', r['accessNote'])

    def test_public_body_and_navigation(self):
        html = TITLE + '<h2>1、梦的开始 简介...</h2><ul class="content_ul"><li>' + '安全正文。<br>' * 50 + '</li></ul><aside>推荐小说</aside><a href="/book2/6407777/2">下一章</a>'
        r = reader.parse_page(html, BASE + '/1')
        self.assertEqual(r['title'], '第1章 梦的开始')
        self.assertEqual(len(r['paragraphs']), 50)
        self.assertEqual(r['next'], BASE + '/2')
        self.assertNotIn('推荐小说', ''.join(r['paragraphs']))

    def test_vip_boundary_has_no_automatic_next_request(self):
        html = TITLE + '<h2>23、秋后算账</h2><ul class="content_ul"><li>' + '安全正文。<br>' * 50 + '</li></ul><a href="/vip/6407777/24">下一章</a>'
        r = reader.parse_page(html, BASE + '/23')
        self.assertIsNone(r['next'])
        self.assertIn('/vip/', r['restrictedNext'])

    def test_direct_vip_import_stops_before_fetch(self):
        with patch('reader.fetch_page') as fetch, self.assertRaises(reader.ReaderError):
            reader.load_reading('https://wap.jjwxc.net/vip/6407777/24')
        fetch.assert_not_called()

    def test_missing_body_fails_without_recommendation_fallback(self):
        with self.assertRaises(reader.ReaderError):
            reader.parse_page(TITLE + '<div>' + '推荐正文' * 500 + '</div>', BASE + '/1')

    def test_previous_vip_link_is_not_reported_as_next(self):
        html = TITLE + '<h2>26、标题</h2><ul class="content_ul"><li>' + '测试正文。<br>' * 50 + '</li></ul><a href="/vip/6407777/25">上一章</a>'
        r = reader.parse_page(html, BASE + '/26')
        self.assertNotIn('restrictedNext', r)
        self.assertIsNone(r['previous'])


if __name__ == '__main__':
    unittest.main()