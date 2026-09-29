import tempfile
import unittest
from pathlib import Path
from dotenv import dotenv_values

from profile_registry import add_profile


class ProfileRegistryTests(unittest.TestCase):
    def test_add_named_profile_updates_env_and_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('ROLLOVER_PROFILES=default\n', encoding='utf-8')
            env = {'ROLLOVER_PROFILES': 'default', 'MEXC_P1_API_KEY': 'P1KEY',
                   'MEXC_P1_MEMBER_ID': 'P1', 'MEXC_P2_API_KEY': 'OLDKEY',
                   'MEXC_P2_MEMBER_ID': 'OLD'}
            values = ['new', 'NEWKEY', 'SECRET', 'NEW_MEMBER', 'NewNick', '2642995']
            self.assertEqual(add_profile(path, values, env), 'new')
            self.assertIn('MEXC_P2_NEW_SECRET_KEY', path.read_text(encoding='utf-8'))
            self.assertEqual(dotenv_values(path)['MEXC_P2_NEW_SECRET_KEY'], 'SECRET')
            self.assertEqual(dotenv_values(path)['ROLLOVER_PROFILES'], 'default,new')
            self.assertEqual(env['ROLLOVER_PROFILES'], 'default,new')
            with self.assertRaises(ValueError):
                add_profile(path, values, env)

    def test_invalid_profile_does_not_modify_env(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('ROLLOVER_PROFILES=default\n', encoding='utf-8')
            env = {'ROLLOVER_PROFILES': 'default'}
            with self.assertRaises(ValueError):
                add_profile(path, ['bad name', 'KEY', 'SECRET', 'MEMBER', 'Nick', '578'], env)
            self.assertEqual(path.read_text(encoding='utf-8'), 'ROLLOVER_PROFILES=default\n')

    def test_optional_proxy_is_saved_with_new_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('ROLLOVER_PROFILES=default\n', encoding='utf-8')
            env = {'ROLLOVER_PROFILES': 'default'}
            add_profile(path, ['new', 'KEY', 'SECRET', 'MEMBER', 'Nick', '2642995',
                               'http://proxy.example:8080'], env)
            self.assertEqual(dotenv_values(path)['MEXC_P2_NEW_PROXY_URL'], 'http://proxy.example:8080')
            self.assertEqual(env['MEXC_P2_NEW_PROXY_URL'], 'http://proxy.example:8080')
