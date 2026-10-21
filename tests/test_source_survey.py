import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location('source_survey', Path(__file__).with_name('source-survey.py'))
survey = importlib.util.module_from_spec(spec)
spec.loader.exec_module(survey)


class SourceSurveyTests(unittest.TestCase):
    def test_metadata_is_not_acquisition(self):
        result = survey.summarize('gutendex', '{"count":1,"next":"next","results":[{"title":"书",'
                                  '"formats":{"text/plain":"https://example.org/book.txt"}}]}', '')
        self.assertEqual(result['fileCandidates'], 1)
        self.assertTrue(result['hasMore'])
        self.assertFalse(result['acquisitionVerified'])

    def test_zlibrary_links_and_no_private_urls(self):
        result = survey.summarize('z-library', '<h3 itemprop="name">书</h3>'
                                  '<a href="/book/1?token=private">版本</a>'
                                  '<a href="https://other.example/download">外链</a>'
                                  '<a href="/zh/login">登录</a>', 'https://z-lib.tv/zh/s?q=test')
        self.assertEqual(result['detailLinkCount'], 1)
        self.assertEqual(result['downloadLinkCount'], 0)
        self.assertTrue(result['loginPresent'])
        self.assertNotIn('private', str(result))
        self.assertFalse(result['acquisitionVerified'])

    def test_fixed_endpoints_only(self):
        with self.assertRaises(ValueError):
            survey.fetch_structured('https://127.0.0.1/private')

    def test_wikisource_and_opds_are_candidates(self):
        result = survey.summarize('wikisource', '{"query":{"search":[{"title":"紅樓夢"}]}}', '')
        self.assertEqual(result['titles'], ['紅樓夢'])
        self.assertFalse(result['acquisitionVerified'])
        result = survey.summarize('standardebooks', '<feed xmlns="http://www.w3.org/2005/Atom">'
                                  '<entry><link rel="http://opds-spec.org/acquisition/open-access" /></entry></feed>', '')
        self.assertEqual(result['acquisitionLinkCount'], 1)
        self.assertFalse(result['acquisitionVerified'])