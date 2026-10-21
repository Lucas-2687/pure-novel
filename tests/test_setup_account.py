import os
import unittest
from unittest.mock import patch

import cloud
import setup_account


class SetupAccountTests(unittest.TestCase):
    def test_invalid_project_never_requests_secret(self):
        for url in ('http://example.com', 'https://example.com/path', 'https://example.com:bad', 'https://[invalid'):
            with self.subTest(url=url), patch('builtins.print'), patch('builtins.input', return_value=url), patch('getpass.getpass') as secret:
                with self.assertRaises(cloud.CloudError):
                    setup_account.main()
                secret.assert_not_called()

    def test_creates_chinese_account_and_restores_environment(self):
        with patch.dict(os.environ, {'SUPABASE_URL': 'previous'}, clear=True), patch('builtins.print'), patch('builtins.input', side_effect=['https://example.supabase.co', '\u6d4b\u8bd5\u8bfb\u8005']), patch('getpass.getpass', side_effect=['sb_secret_test', 'a-long-password', 'a-long-password']), patch('manage_user.create_user', return_value={'id': 'uid', 'username': '测试读者'}) as create:
            setup_account.main()
            create.assert_called_once_with('测试读者', 'a-long-password')
            self.assertEqual(dict(os.environ), {'SUPABASE_URL': 'previous'})

    def test_mismatch_does_not_create(self):
        with patch('builtins.print'), patch('builtins.input', side_effect=['https://example.supabase.co', '\u6d4b\u8bd5\u8bfb\u8005']), patch('getpass.getpass', side_effect=['sb_secret_test', 'a-long-password', 'different']), patch('manage_user.create_user') as create:
            with self.assertRaises(cloud.CloudError):
                setup_account.main()
            create.assert_not_called()

    def test_failure_cleans_up_credentials(self):
        with patch.dict(os.environ, {}, clear=True), patch('builtins.print'), patch('builtins.input', side_effect=['https://example.supabase.co', '\u6d4b\u8bd5\u8bfb\u8005']), patch('getpass.getpass', side_effect=['sb_secret_test', 'a-long-password', 'a-long-password']), patch('manage_user.create_user', side_effect=cloud.CloudError(503, '失败')):
            with self.assertRaises(cloud.CloudError):
                setup_account.main()
            self.assertNotIn('SUPABASE_SECRET_KEY', os.environ)