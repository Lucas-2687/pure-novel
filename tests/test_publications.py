import unittest
import publications
import reader


class PublicationsTests(unittest.TestCase):
    def test_metadata_only_and_versions(self):
        html = ''.join(f'<table><tr><td><h3 itemprop="name">测试书</h3><a href="/author/a">作者甲</a><p>Language: Chinese File: {fmt} Year: 2020</p></td></tr></table>' for fmt in ('EPUB', 'PDF'))
        result = publications.parse_results(html, 'https://z-lib.tv/zh/s?q=test', reader.Document, reader.tidy)
        self.assertEqual(len(result['books']), 2)
        self.assertEqual(result['books'][0]['format'], 'EPUB')
        self.assertFalse(result['canDownload'])
        self.assertEqual(result['books'][0]['authors'], ['作者甲'])

    def test_no_page_wide_fallback(self):
        result = publications.parse_results('<h3 itemprop="name">无容器</h3>', 'https://z-lib.tv/zh', reader.Document, reader.tidy)
        self.assertEqual(result['books'], [])

    def test_external_redirect_rejected(self):
        with self.assertRaises(ValueError):
            publications.parse_results('', 'https://other.example/', reader.Document, reader.tidy)

    def test_invalid_query(self):
        with self.assertRaises(ValueError):
            publications.search('', None, None, None)