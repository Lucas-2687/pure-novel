import json
from pathlib import Path
import shlex
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RenderConfigurationTests(unittest.TestCase):
    def setUp(self):
        # JSON is a YAML subset; this deployment file needs no extra parser.
        self.config = json.loads((ROOT / 'render.yaml').read_text(encoding='utf-8'))
        self.service = self.config['services'][0]

    def test_only_one_free_service_without_paid_resources(self):
        self.assertEqual(set(self.config), {'services'})
        self.assertEqual(len(self.config['services']), 1)
        self.assertEqual(self.service['type'], 'web')
        self.assertEqual(self.service['runtime'], 'python')
        self.assertEqual(self.service['plan'], 'free')
        self.assertEqual(self.service['region'], 'singapore')
        for paid_feature in ('disk', 'scaling', 'preDeployCommand', 'customDomains'):
            self.assertNotIn(paid_feature, self.service)

    def test_preview_branch_and_manual_deploys_preserve_existing_site(self):
        self.assertEqual(self.service['repo'], 'https://github.com/Lucas-2687/pure-novel')
        self.assertEqual(self.service['branch'], 'deploy/render-free-trial')
        self.assertEqual(self.service['autoDeployTrigger'], 'off')
        self.assertEqual(self.service['healthCheckPath'], '/api/health')

    def test_start_command_uses_platform_port_and_same_origin_backend(self):
        self.assertEqual(shlex.split(self.service['startCommand']),
                         ['python', 'reader.py', '--host', '0.0.0.0', '--port', '$PORT'])
        self.assertNotIn('vercel', self.service['startCommand'])

    def test_trial_is_authenticated_without_any_database_credentials(self):
        env = {entry['key']: entry.get('value') for entry in self.service['envVars']}
        self.assertEqual(len(env), len(self.service['envVars']))
        self.assertEqual(env, {'REQUIRE_AUTH': '1', 'COOKIE_SECURE': '1',
                               'READER_RPC_V2': '1', 'PYTHONUNBUFFERED': '1'})
        self.assertFalse(any('SUPABASE' in key or 'TOKEN' in key or 'SECRET' in key for key in env))
        self.assertTrue(all(set(entry) == {'key', 'value'} for entry in self.service['envVars']))

    def test_build_checks_real_sources_without_installing_frameworks(self):
        command = shlex.split(self.service['buildCommand'])
        self.assertEqual(command[:4], ['python', '-m', 'compileall', '-q'])
        self.assertIn('reader.py', command[4:])
        for filename in command[4:]:
            self.assertTrue((ROOT / filename).is_file(), filename)
        self.assertEqual(self.service['runtime'], 'python')
