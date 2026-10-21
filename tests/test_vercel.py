import json
from pathlib import Path
import unittest

from api.index import handler
from reader import Handler


class VercelTests(unittest.TestCase):
    def test_explicit_handler_inherits_reader(self):
        self.assertTrue(issubclass(handler, Handler))

    def test_python_bundle_includes_static_resources(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / 'vercel.json').read_text(encoding='utf-8'))
        build = config['builds'][0]
        self.assertEqual(build['src'], 'api/index.py')
        self.assertEqual(build['use'], '@vercel/python')
        self.assertIn('public/**', build['config']['includeFiles'])
        self.assertEqual(config['routes'][0]['dest'], '/api/index.py')