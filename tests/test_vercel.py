import json
from pathlib import Path
import re
import unittest

from api.index import handler
from reader import Handler

ROOT = Path(__file__).resolve().parents[1]


class VercelTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'vercel.json').read_text(encoding='utf-8'))

    def test_explicit_handler_inherits_reader(self):
        self.assertTrue(issubclass(handler, Handler))

    def test_static_output_is_separate_from_python(self):
        self.assertEqual(self.config['outputDirectory'], 'public')
        self.assertIsNone(self.config['framework'])
        self.assertEqual(self.config['regions'], ['hnd1'])
        self.assertNotIn('builds', self.config)
        self.assertNotIn('routes', self.config)
        self.assertEqual(self.config['rewrites'], [{'source': '/api/:path*', 'destination': '/api'}])
        options = self.config['functions']['api/index.py']
        self.assertEqual(options['maxDuration'], 60)
        self.assertEqual(options['includeFiles'], 'public/**')
        for unnecessary in ('tests/**', 'supabase/**', '.venv/**', '*.log', '.env*',
                            'acquire.py', 'manage_user.py', 'setup_account.py',
                            'requirements-assistant.txt', 'search-validation.json', 'source-survey.json'):
            self.assertIn(unnecessary, options['excludeFiles'])

    def test_all_script_and_shell_assets_are_published(self):
        html = (ROOT / 'public' / 'index.html').read_text(encoding='utf-8')
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
        self.assertIn('/storage.js', scripts)
        shell = (ROOT / 'public' / 'sw.js').read_text(encoding='utf-8')
        assets = re.search(r'const SHELL = \[([^\]]+)\]', shell).group(1)
        paths = re.findall(r"'([^']+)'", assets)
        for asset in set(paths + scripts):
            with self.subTest(asset=asset):
                file = ROOT / 'public' / ('index.html' if asset == '/' else asset.lstrip('/'))
                self.assertTrue(file.is_file())
                self.assertNotIn('/api/', asset)
        published = {p.suffix for p in (ROOT / 'public').iterdir() if p.is_file()}
        self.assertFalse(published & {'.py', '.env', '.sql', '.log'})

    def test_api_never_uses_shared_caching(self):
        entries = self.config['headers']
        api = next(h['headers'] for h in entries if h['source'] == '/api/:path*')
        headers = {h['key'].lower(): h['value'] for h in api}
        for key in ('cache-control', 'cdn-cache-control', 'vercel-cdn-cache-control'):
            self.assertIn('no-store', headers[key])
        static = next(h['headers'] for h in entries if h['source'].startswith('/((?!api'))
        self.assertNotIn('immutable', static[0]['value'])
        self.assertIn('must-revalidate', static[0]['value'])
        sw = next(h['headers'] for h in entries if h['source'] == '/sw.js')
        self.assertEqual({h['key']: h['value'] for h in sw}['Cache-Control'], 'no-cache')

    def test_static_responses_keep_security_headers(self):
        all_headers = next(h['headers'] for h in self.config['headers'] if h['source'] == '/(.*)')
        headers = {h['key'].lower(): h['value'] for h in all_headers}
        self.assertEqual(headers['x-content-type-options'], 'nosniff')
        self.assertEqual(headers['referrer-policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", headers['content-security-policy'])