import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings, p2_prefix, proxy_url, select_p2_profile
from cycle import run_command
from main import async_main, build_parser
from trade_profiles import eflp_p1_fiat, eflp_p2_payment_id, mode_pay_method_id
import test_auto as fixtures


class ProfileTests(unittest.TestCase):
    def test_eflp_fiat_selects_only_its_own_payment_id(self):
        env = {'MEXC_P1_3_FIAT': 'gel',
               'MEXC_P2_12_PAYMENT_ID': '999',
               'MEXC_P2_12_PAYMENT_ID_GEL': '101',
               'MEXC_P2_12_PAYMENT_ID_KZT': '202',
               'MEXC_P2_12_PAYMENT_ID_TJS': '303',
               'MEXC_P2_12_PAYMENT_ID_KGS': '404'}
        self.assertEqual(eflp_p1_fiat('p1_3', env), 'GEL')
        for fiat, expected in [('GEL', '101'), ('KZT', '202'),
                               ('TJS', '303'), ('KGS', '404')]:
            self.assertEqual(eflp_p2_payment_id('12', fiat, env), expected)
        del env['MEXC_P2_12_PAYMENT_ID_GEL']
        with self.assertRaisesRegex(ValueError, 'MEXC_P2_12_PAYMENT_ID_GEL'):
            eflp_p2_payment_id('12', 'GEL', env)
        env['MEXC_P2_12_PAYMENT_ID_GEL'] = '0'
        with self.assertRaisesRegex(ValueError, 'MEXC_P2_12_PAYMENT_ID_GEL'):
            eflp_p2_payment_id('12', 'GEL', env)

    def test_common_pay_method_is_shared_by_p1_and_p2_for_fiat(self):
        env = {'EFLP_PAY_METHOD_ID_KZT': '520', 'EFLP_PAY_METHOD_ID_GEL': '519'}
        self.assertEqual(mode_pay_method_id('eflp_volume', 'KZT', env), '520')
        self.assertEqual(mode_pay_method_id('eflp_unique', 'GEL', env), '519')
        with self.assertRaisesRegex(ValueError, 'EFLP_PAY_METHOD_ID_TJS'):
            mode_pay_method_id('eflp_volume', 'TJS', env)

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
    async def test_payment_ids_reads_selected_cash_profiles_in_order(self):
        env = {'ADSPOWER_API_KEY': 'test-key', 'MEXC_PAY_METHOD_ID': '578',
               'MEXC_P2_3_ADSPOWER_PROFILE_ID': 'browser-3',
               'MEXC_P2_12_ADSPOWER_PROFILE_ID': 'browser-12'}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch('sys.argv', ['main.py', 'payment-ids', '--mode', 'cash',
                                   '--profiles', '3,12']), \
                patch('payment_ids.AdsPower') as browser_factory, redirect_stdout(output):
            browser_factory.return_value.payment_account_by_method = AsyncMock(
                side_effect=[3003, 3012])
            self.assertEqual(await async_main(), 0)
        self.assertEqual([call.args[2] for call in browser_factory.call_args_list],
                         ['browser-3', 'browser-12'])
        self.assertEqual(browser_factory.return_value.payment_account_by_method.await_args_list[0].args,
                         (578, 'RUB'))
        self.assertIn('MEXC_P2_3_PAYMENT_ID=3003', output.getvalue())
        self.assertIn('MEXC_P2_12_PAYMENT_ID=3012', output.getvalue())

    async def test_payment_ids_eflp_reports_one_failure_and_continues(self):
        from adspower import AdsPowerError

        env = {'ADSPOWER_API_KEY': 'test-key', 'EFLP_PAY_METHOD_ID_KZT': '520',
               'MEXC_P2_3_ADSPOWER_PROFILE_ID': 'browser-3',
               'MEXC_P2_12_ADSPOWER_PROFILE_ID': 'browser-12'}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch('sys.argv', ['main.py', 'payment-ids', '--mode', 'eflp',
                                   '--fiat', 'KZT', '--profiles', '3,12']), \
                patch('payment_ids.AdsPower') as browser_factory, redirect_stdout(output):
            browser_factory.return_value.payment_account_by_method = AsyncMock(
                side_effect=[AdsPowerError('способ оплаты не найден'), 3012])
            self.assertEqual(await async_main(), 1)
        self.assertIn('П2 3: ошибка', output.getvalue())
        self.assertIn('MEXC_P2_12_PAYMENT_ID_KZT=3012', output.getvalue())
        self.assertEqual(browser_factory.return_value.payment_account_by_method.await_count, 2)

    async def test_payment_ids_accepts_ads_id_when_p2_browser_is_not_in_env(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'ADSPOWER_API_KEY': 'test-key'}, clear=True), \
                patch('sys.argv', ['main.py', 'payment-ids', '--mode', 'cash',
                                   '--profiles', '3=k1example']), \
                patch('payment_ids.AdsPower') as browser_factory, redirect_stdout(output):
            browser_factory.return_value.payment_account_by_method = AsyncMock(return_value=3003)
            self.assertEqual(await async_main(), 0)
        browser_factory.assert_called_once_with('http://127.0.0.1:50325', 'test-key', 'k1example')
        self.assertIn('MEXC_P2_3_PAYMENT_ID=3003', output.getvalue())

    async def test_payment_method_check_reads_selected_p2_without_order(self):
        env = {'EFLP_PAY_METHOD_ID_KZT': '520',
               'MEXC_P2_12_ADSPOWER_PROFILE_ID': 'browser-p2',
               'ADSPOWER_API_KEY': 'test-key'}
        with patch.dict(os.environ, env, clear=True), \
                patch('sys.argv', ['main.py', 'payment-method-check', '--mode', 'eflp',
                                   '--p2-profile', '12', '--fiat', 'KZT']), \
                patch('adspower.AdsPower') as browser_factory, redirect_stdout(io.StringIO()):
            browser_factory.return_value.payment_account_by_method = AsyncMock(return_value=2253483)
            self.assertEqual(await async_main(), 0)
        browser_factory.assert_called_once_with('http://127.0.0.1:50325', 'test-key', 'browser-p2')
        browser_factory.return_value.payment_account_by_method.assert_awaited_once_with(520, 'KZT')

    async def test_cash_cycle_uses_p2_payment_id_without_p2_browser(self):
        with TemporaryDirectory() as directory:
            env = {'CYCLE_DB': str(Path(directory) / 'cycles.sqlite3'),
                   'ENABLE_STATE_CHANGES': 'true', 'SELLER_CHECK_MODE': 'adspower',
                   'MEXC_P1_API_KEY': 'p1-key', 'MEXC_P1_SECRET_KEY': 'p1-secret',
                   'MEXC_P1_MEMBER_ID': 'p1-member',
                   'MEXC_P1_SELL_ADV_NO': 'a1234567890123456789',
                   'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788',
                   'ADSPOWER_P1_PROFILE_ID': 'browser-p1',
                   'MEXC_P2_12_API_KEY': 'p2-key', 'MEXC_P2_12_SECRET_KEY': 'p2-secret',
                   'MEXC_P2_12_MEMBER_ID': 'p2-member', 'MEXC_P2_12_NICKNAME': 'P2',
                   'MEXC_P2_12_PAYMENT_ID': '999', 'MEXC_PAY_METHOD_ID': '578'}
            captured = []
            async def series(runner, cycle_id):
                captured.append((runner, runner.journal.cycle(cycle_id)))
            with (patch.dict(os.environ, env, clear=True), patch('logger_setup.setup_logging'),
                  patch('mexc_client.MexcP2PClient') as client,
                  patch('adspower.AdsPower') as ads,
                  patch('cycle.run_series', side_effect=series),
                  redirect_stdout(io.StringIO())):
                client.return_value.close = AsyncMock()
                browser = ads.return_value
                browser.profile_id = 'browser-p1'
                browser.ensure_started = AsyncMock()
                browser.ensure_mexc_page = AsyncMock()
                browser.connection.return_value.__aenter__.return_value = AsyncMock(return_value={})
                ads.from_env.return_value = browser
                args = build_parser().parse_args([
                    'cycle', '--auto', '--scheduler-mode', 'cash_volume',
                    '--p2-profile', '12', '--amount', '1000 RUB'])
                self.assertEqual(await run_command(args), 0)
                self.assertEqual(captured[-1][0].p2_payment_id, '999')
                self.assertIsNone(captured[-1][0].p2_pay_method_id)
                self.assertIsNone(captured[-1][0].p2_payment_browser)
                self.assertEqual(captured[-1][1]['spec']['p2_payment_id'], '999')
                ads.from_env.assert_called_once()
                ads.assert_not_called()

    async def test_eflp_cycle_uses_saved_fiat_payment_id_without_p2_browser(self):
        with TemporaryDirectory() as directory:
            env = {'CYCLE_DB': str(Path(directory) / 'cycles.sqlite3'),
                   'ENABLE_STATE_CHANGES': 'true', 'SELLER_CHECK_MODE': 'adspower',
                   'MEXC_P1_3_API_KEY': 'p1-key', 'MEXC_P1_3_SECRET_KEY': 'p1-secret',
                   'MEXC_P1_3_MEMBER_ID': 'p1-member', 'MEXC_P1_3_NICKNAME': 'P1 GEL',
                   'MEXC_P1_3_SELL_ADV_NO': 'a1234567890123456789',
                   'MEXC_P1_3_BUY_ADV_NO': 'a1234567890123456788',
                   'MEXC_P1_3_ADSPOWER_PROFILE_ID': 'browser-gel',
                   'MEXC_P1_3_FIAT': 'GEL', 'EFLP_PAY_METHOD_ID_GEL': '518',
                   'MEXC_P2_12_API_KEY': 'p2-key', 'MEXC_P2_12_SECRET_KEY': 'p2-secret',
                   'MEXC_P2_12_MEMBER_ID': 'p2-member', 'MEXC_P2_12_NICKNAME': 'P2',
                   'MEXC_P2_12_PAYMENT_ID': '999',
                   'MEXC_P2_12_PAYMENT_ID_GEL': '12345'}
            captured = []
            async def series(runner, cycle_id):
                captured.append((runner, runner.journal.cycle(cycle_id)))
            with (patch.dict(os.environ, env, clear=True), patch('logger_setup.setup_logging'),
                  patch('mexc_client.MexcP2PClient') as client,
                  patch('adspower.AdsPower') as ads,
                  patch('cycle.run_series', side_effect=series),
                  redirect_stdout(io.StringIO())):
                client.return_value.close = AsyncMock()
                def make_browser(base_url, api_key, profile_id):
                    browser = MagicMock()
                    browser.profile_id = profile_id
                    browser.ensure_started = AsyncMock()
                    browser.ensure_mexc_page = AsyncMock()
                    browser.payment_account_by_method = AsyncMock(return_value=12345)
                    browser.connection.return_value.__aenter__.return_value = AsyncMock(return_value={})
                    return browser
                ads.side_effect = make_browser
                args = build_parser().parse_args([
                    'cycle', '--auto', '--scheduler-mode', 'eflp_volume',
                    '--p1-profile', 'p1_3', '--p2-profile', '12', '--amount', '1000 GEL'])
                self.assertEqual(await run_command(args), 0)
                self.assertEqual(captured[-1][0].p2_payment_id, '12345')
                self.assertIsNone(captured[-1][0].p2_pay_method_id)
                self.assertIsNone(captured[-1][0].p2_payment_browser)
                self.assertEqual(captured[-1][1]['spec']['p2_payment_id'], '12345')
                self.assertEqual(captured[-1][1]['spec']['eflp_p1_pay_method_id'], 518)
                self.assertEqual(captured[-1][1]['spec']['fiat'], 'GEL')
                self.assertEqual(ads.call_count, 1)
                os.environ['EFLP_PAY_METHOD_ID_GEL'] = '54321'
                os.environ['MEXC_P2_12_PAYMENT_ID_GEL'] = '99999'
                resume = build_parser().parse_args(['cycle', '--resume', captured[-1][1]['id']])
                self.assertEqual(await run_command(resume), 0)
                self.assertEqual(captured[-1][0].p2_payment_id, '12345')
                self.assertEqual(captured[-1][1]['spec']['eflp_p1_pay_method_id'], 518)
                self.assertEqual(ads.call_count, 2)

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
