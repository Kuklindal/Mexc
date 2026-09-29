import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from config import Settings, p2_prefix, proxy_url, select_p2_profile
from cycle import run_command
from main import build_parser
import test_auto as fixtures


class ProfileTests(unittest.TestCase):
    def test_default_and_named_profiles_are_isolated(self):
        env = {'MEXC_P1_API_KEY': 'p1-key', 'MEXC_P1_SECRET_KEY': 'p1-secret',
               'MEXC_P2_API_KEY': 'old-key', 'MEXC_P2_SECRET_KEY': 'old-secret',
               'MEXC_P2_2_API_KEY': 'second-key', 'MEXC_P2_2_SECRET_KEY': 'second-secret',
               'MEXC_P2_PROFILE': '2'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Settings.from_env('p1').api_key, 'p1-key')
            self.assertEqual(Settings.from_env('p2').api_key, 'second-key')
            self.assertEqual(Settings.from_env('p2', p2_profile='default').api_key, 'old-key')
            second = Settings.from_env('p2', p2_profile='2')
            self.assertEqual((second.api_key, second.secret_key), ('second-key', 'second-secret'))
            with self.assertRaises(RuntimeError):
                Settings.from_env('p2', p2_profile='missing')
            with self.assertRaises(ValueError):
                Settings.from_env('p1', p2_profile='2')
            del os.environ['MEXC_P2_2_SECRET_KEY']
            with self.assertRaises(RuntimeError):
                Settings.from_env('p2', p2_profile='2')

    def test_resume_uses_saved_profile_not_current_default(self):
        self.assertEqual(select_p2_profile(None, {'p2_profile': '2'}, {'MEXC_P2_PROFILE': '3'}), '2')
        self.assertEqual(select_p2_profile(None, {}, {'MEXC_P2_PROFILE': '2'}), 'default')
        with self.assertRaises(ValueError):
            select_p2_profile('3', {'p2_profile': '2'}, {})
        self.assertEqual(select_p2_profile('2', None, {'MEXC_P2_PROFILE': '3'}), '2')
        self.assertEqual(p2_prefix('Friend'), 'MEXC_P2_FRIEND')
        for name in ('', '../p1', 'a-b', 'a' * 33):
            with self.subTest(name=name), self.assertRaises(ValueError):
                p2_prefix(name)

    def test_each_p2_reads_only_its_own_proxy(self):
        env = {'MEXC_P2_API_KEY': 'default-key', 'MEXC_P2_SECRET_KEY': 'default-secret',
               'MEXC_P2_PROXY_URL': 'http://proxy-default.example:8080',
               'MEXC_P2_2_API_KEY': 'second-key', 'MEXC_P2_2_SECRET_KEY': 'second-secret',
               'MEXC_P2_2_PROXY_URL': 'socks5://proxy-second.example:1080'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Settings.from_env('p2', p2_profile='default').proxy_url,
                             env['MEXC_P2_PROXY_URL'])
            self.assertEqual(Settings.from_env('p2', p2_profile='2').proxy_url,
                             env['MEXC_P2_2_PROXY_URL'])
            self.assertIsNone(Settings.from_env('p1', require_keys=False).proxy_url)
        for invalid in ('ftp://proxy.example:8080', 'http://proxy.example',
                        'http://proxy.example:8080/path', 'http://proxy.example:bad'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                proxy_url(invalid, 'PROXY')


class ProfileCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_binds_keys_identity_payment_and_persists_selection(self):
        with TemporaryDirectory() as directory:
            env = {'CYCLE_DB': str(Path(directory) / 'cycles.sqlite3'), 'ENABLE_STATE_CHANGES': 'true',
                   'MEXC_P1_API_KEY': 'p1-key', 'MEXC_P1_SECRET_KEY': 'p1-secret', 'MEXC_P1_MEMBER_ID': 'ID1',
                   'MEXC_P1_SELL_ADV_NO': 'AD-SELL', 'MEXC_P1_BUY_ADV_NO': 'AD-BUY',
                   'MEXC_P2_API_KEY': 'wrong-default-key', 'MEXC_P2_SECRET_KEY': 'wrong-secret',
                   'MEXC_P2_MEMBER_ID': 'wrong-member', 'MEXC_P2_NICKNAME': 'wrong-name', 'MEXC_P2_PAYMENT_ID': '111',
                   'MEXC_P2_2_API_KEY': 'p2-second-key', 'MEXC_P2_2_SECRET_KEY': 'p2-second-secret',
                   'MEXC_P2_2_PROXY_URL': 'http://proxy-second.example:8080',
                   'MEXC_P2_2_MEMBER_ID': 'ID2', 'MEXC_P2_2_NICKNAME': 'Second', 'MEXC_P2_2_PAYMENT_ID': '222'}
            captured = []
            async def series(runner, cycle_id):
                captured.append((runner, runner.journal.cycle(cycle_id)))
            with patch.dict(os.environ, env, clear=True), patch('logger_setup.setup_logging'), \
                    patch('mexc_client.MexcP2PClient') as client, patch('cycle.run_series', side_effect=series), \
                    redirect_stdout(io.StringIO()):
                client.return_value.close = AsyncMock()
                args = build_parser().parse_args(['cycle', '--p2-profile', '2', '--amount', '1000 RUB'])
                self.assertEqual(await run_command(args), 0)
                runner, saved = captured[0]
                self.assertEqual(runner.trusted_members, {'p1': 'ID1', 'p2': 'ID2'})
                self.assertEqual(runner.trusted_nicknames['p2'], 'Second')
                self.assertEqual(runner.p2_payment_id, '222')
                self.assertEqual(saved['spec']['p2_profile'], '2')
                self.assertEqual(saved['spec']['p2_payment_id'], '222')
                self.assertNotIn('p2-second-key', json.dumps(saved['spec']))
                self.assertEqual(client.call_args_list[0].args[:2], ('p1-key', 'p1-secret'))
                self.assertEqual(client.call_args_list[1].args[:2], ('p2-second-key', 'p2-second-secret'))
                self.assertEqual(client.call_args_list[1].kwargs['proxy_url'], 'http://proxy-second.example:8080')
                resume = build_parser().parse_args(['cycle', '--resume', saved['id']])
                self.assertEqual(await run_command(resume), 0)
                self.assertEqual(captured[-1][0].p2_profile, '2')
                self.assertEqual(client.call_args_list[-1].args[:2], ('p2-second-key', 'p2-second-secret'))
                resume.p2_profile = 'default'
                with self.assertRaises(ValueError):
                    await run_command(resume)


class ProfileBindingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown
    runner = fixtures.AutoTests.runner

    async def test_saved_profile_identity_and_requisites_cannot_change(self):
        for changed, value in [('p2_profile', '2'), ('p2_payment_id', '999'),
                               ('members', {'p1': 'MEMBER-P1', 'p2': 'OTHER'}),
                               ('nicknames', {'p2': 'OTHER'})]:
            with self.subTest(field=changed):
                runner = self.runner()
                spec = dict(self.spec, p2_profile='default', p2_payment_id='2642995',
                            members=runner.trusted_members, nicknames=runner.trusted_nicknames)
                spec[changed] = value
                self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(spec), self.cycle_id))
                self.journal.db.commit()
                with self.assertRaises(ValueError):
                    await runner.run(self.cycle_id)
                self.assertEqual(self.exchange.calls, [])
                runner.browser.approve.assert_not_called()
