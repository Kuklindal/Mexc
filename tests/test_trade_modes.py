import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch

from cycle import CashVolumeLimitReached, OperatorStopped, Paused, Step, steps_for_spec
from adspower import AdsPowerTimeout, AdsPowerUnavailable
from mexc_client import MexcChatUnavailable, MexcReadUnavailable
from rollover import load_state, save_state
from trade_modes import (_eflp_target_reached, _finish_cash_network_return, _finish_completed_cycle,
                           _finish_terminal_cycle,
                           _handle_forward_rejection, _record_forward,
                           begin_mode, choose_mode_amount, configured_mode_profiles,
                           next_profile, restore_eflp_progress, run_mode)
from volume_policy import record_purchase
import test_auto as fixtures


def env_for(count=20):
    env = {
        'ROLLOVER_PROFILES': ','.join(str(i) for i in range(1, count + 1)),
        'MEXC_P1_API_KEY': 'main-key', 'MEXC_P1_SECRET_KEY': 'main-secret',
        'MEXC_P1_MEMBER_ID': 'main-member', 'MEXC_P1_SELL_ADV_NO': 'a1234567890123456789',
        'MEXC_P1_FIAT': 'RUB',
        'EFLP_PAY_METHOD_ID_RUB': '518',
        'EFLP_PAY_METHOD_ID_GEL': '519',
        'EFLP_PAY_METHOD_ID_KZT': '520',
        'EFLP_PAY_METHOD_ID_TJS': '521',
        'EFLP_PAY_METHOD_ID_KGS': '522',
        'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40,
        'ADSPOWER_P1_PROFILE_ID': 'main-browser',
    }
    for index in range(1, count + 1):
        prefix = f'MEXC_P2_{index}'
        env.update({f'{prefix}_API_KEY': f'key-{index}',
                    f'{prefix}_SECRET_KEY': f'secret-{index}',
                     f'{prefix}_MEMBER_ID': f'member-{index}',
                     f'{prefix}_NICKNAME': f'name-{index}',
                     f'{prefix}_PAYMENT_ID': str(1000 + index),
                     f'{prefix}_PAYMENT_ID_RUB': str(1000 + index),
                     f'{prefix}_SELL_ADV_NO': f'a{index:019d}',
                    f'{prefix}_ADSPOWER_PROFILE_ID': f'browser-{index}'})
    return env


class TradeModeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown

    def complete(self, cycle_id, quantity='1800'):
        self.journal.transition(cycle_id, 'reverse_complete', 'both', 'done', 'returned',
                                result={'quantity': quantity})
        self.journal.transition(cycle_id, 'reverse_replenish', 'p1', 'done', 'refilled',
                                result={'quantity': quantity})
        self.journal.transition(cycle_id, 'cycle', 'both', 'completed', 'done',
                                cycle_status='completed')

    def test_fixed_mode_profiles_use_env_keys_and_preserve_order(self):
        env = env_for(3) | {'CASH_VOLUME_P1_PROFILE': 'P1',
                            'CASH_VOLUME_P2_PROFILES': '3,1',
                            'EFLP_VOLUME_P2_PROFILES': '2,3',
                            'EFLP_UNIQUE_P2_PROFILES': '1,2,3'}
        self.assertEqual(configured_mode_profiles('cash_volume', env), ('p1', ['3', '1']))
        self.assertEqual(configured_mode_profiles('eflp_volume', env), (None, ['2', '3']))
        self.assertEqual(configured_mode_profiles('eflp_unique', env), (None, ['1', '2', '3']))
        env['CASH_VOLUME_P1_PROFILE'] = '2'
        self.assertEqual(configured_mode_profiles('cash_volume', env), ('2', ['3', '1']))
        env['CASH_VOLUME_P2_PROFILES'] = '2,3'
        with self.assertRaisesRegex(ValueError, 'одновременно'):
            configured_mode_profiles('cash_volume', env)
        env['CASH_VOLUME_P2_PROFILES'] = '2,2'
        with self.assertRaisesRegex(ValueError, 'несколько раз'):
            configured_mode_profiles('cash_volume', env)
        env['CASH_VOLUME_P2_PROFILES'] = '5'
        with self.assertRaisesRegex(ValueError, 'ROLLOVER_PROFILES'):
            configured_mode_profiles('cash_volume', env)

    async def test_cash_unique_twenty_fifth_cycle_returns_to_second_p1(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(25) | {
            'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788',
            'CASH_UNIQUE_P1_PROFILES': 'p1,p1_2',
            'MEXC_P1_2_API_KEY': 'second-key', 'MEXC_P1_2_SECRET_KEY': 'second-secret',
            'MEXC_P1_2_MEMBER_ID': 'second-member', 'MEXC_P1_2_NICKNAME': 'Second maker',
            'MEXC_P1_2_SELL_ADV_NO': 'a1234567890123456787',
            'MEXC_P1_2_BUY_ADV_NO': 'a1234567890123456786',
            'MEXC_P1_2_ADSPOWER_PROFILE_ID': 'second-browser',
        }
        state = begin_mode(self.journal, 'cash_unique', p1_profile='p1',
                           p2_profiles=[str(i) for i in range(1, 26)], env=env)
        state['unique_done'] = state['profiles'][:24]
        state['cursor'] = 24
        with patch.dict(os.environ, env), patch('trade_modes.choose_mode_amount',
            new_callable=AsyncMock, return_value='100 RUB'), patch('trade_modes.run_command',
            new_callable=AsyncMock) as trade:
            with self.assertRaisesRegex(Paused, 'Цикл не был сохранён'):
                await run_mode(self.journal, state, asyncio.Event(), None)
        args = trade.await_args.args[0]
        self.assertEqual(args.reverse_p1_profile, 'p1_2')
        self.assertEqual(state['terminal_pending']['kind'], 'cash_regular')
        self.assertEqual(state['terminal_pending']['p2'], '25')

    def test_cash_unique_completed_switch_is_saved_once(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(25) | {
            'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788',
            'CASH_UNIQUE_P1_PROFILES': 'p1,p1_2',
            'MEXC_P1_2_API_KEY': 'second-key', 'MEXC_P1_2_SECRET_KEY': 'second-secret',
            'MEXC_P1_2_MEMBER_ID': 'second-member', 'MEXC_P1_2_NICKNAME': 'Second maker',
            'MEXC_P1_2_SELL_ADV_NO': 'a1234567890123456787',
            'MEXC_P1_2_BUY_ADV_NO': 'a1234567890123456786',
            'MEXC_P1_2_ADSPOWER_PROFILE_ID': 'second-browser',
        }
        state = begin_mode(self.journal, 'cash_unique', p1_profile='p1',
                           p2_profiles=[str(i) for i in range(1, 26)], env=env)
        cid = self.journal.create({'mode': 'api', 'scheduler_mode': 'cash_unique',
                                   'p1_profile': 'p1', 'p2_profile': '25',
                                   'reverse_p1_profile': 'p1_2', 'buy_replenish': True})
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
                                result={'quantity': '1800'},
                                context={'amount': '1000', 'quantity': '1800'})
        self.complete(cid)
        self.journal.transition(cid, 'reverse_replenish_buy', 'p1', 'done', 'refilled')
        state['active_cycle'] = cid
        state['terminal_pending'] = {'kind': 'cash_regular', 'from': 'p1', 'to': 'p1_2',
                                     'p2': '25', 'amount': '100 RUB', 'finish': False}
        save_state(self.journal, state)
        with patch.dict(os.environ, env), patch('trade_modes.save_state', wraps=save_state) as save:
            _finish_terminal_cycle(self.journal, state, cid, '25')
        self.assertEqual(save.call_count, 1)
        restored = load_state(self.journal)
        self.assertIsNone(restored['active_cycle'])
        self.assertTrue(restored['terminal_pending']['verified'])
        self.assertEqual(restored['terminal_pending']['cycle_id'], cid)

    def test_finished_week_cannot_start_duplicate_final_order(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'EFLP_P1_PROFILES': 'p1',
                            'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        state['status'] = 'done'
        save_state(self.journal, state)
        with self.assertRaisesRegex(ValueError, 'повторный финальный ордер'):
            begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                       p2_profiles=['1'], env=env)
        with self.assertRaisesRegex(ValueError, 'повторный финальный ордер'):
            begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                       p2_profiles=['1'], env=env)

    async def test_eflp_final_cycle_preserves_reserve_and_has_no_reverse_on_last_p1(self):
        from trade_modes import _final_order
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = {'mode': 'eflp_unique', 'p1_profile': 'p1'}
        ad = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL', 'coinName': 'USDT',
              'advStatus': 'OPEN', 'fiatUnit': 'RUB', 'availableQuantity': '100',
              'price': '100', 'minSingleTransAmount': '100', 'maxSingleTransAmount': '20000'}
        fake = type('Client', (), {'get_ad': AsyncMock(return_value=ad),
                                   'close': AsyncMock()})()
        with (patch('trade_modes.profile_from_env', return_value=type('P', (), {
                'key': 'p1', 'sell_adv_no': env['MEXC_P1_SELL_ADV_NO']})()),
             patch('trade_modes.settings_for_profile', return_value=type('S', (), {
                'api_key': 'x', 'secret_key': 'y', 'base_url': 'https://example.test',
                'recv_window': 1000, 'proxy_url': None})()),
             patch('trade_modes.MexcP2PClient', return_value=fake),
             patch('trade_modes.random.randint', return_value=1100)):
            amount, quantity = await _final_order(state, None, env=env)
        self.assertEqual(quantity, '89.0000')
        self.assertEqual(amount, '8900.00 RUB')

    async def test_final_reserve_must_keep_sell_ad_above_its_minimum(self):
        from trade_modes import _final_order
        env = env_for(1)
        ad = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL', 'coinName': 'USDT',
              'advStatus': 'OPEN', 'fiatUnit': 'RUB', 'availableQuantity': '100',
              'price': '100', 'minSingleTransAmount': '1300', 'maxSingleTransAmount': '20000'}
        fake = type('Client', (), {'get_ad': AsyncMock(return_value=ad),
                                   'close': AsyncMock()})()
        with (patch('trade_modes.settings_for_profile', return_value=type('S', (), {
                'api_key': 'x', 'secret_key': 'y', 'base_url': 'https://example.test',
                'recv_window': 1000, 'proxy_url': None})()),
             patch('trade_modes.MexcP2PClient', return_value=fake)):
            with self.assertRaisesRegex(Paused, '12 USDT'):
                await _final_order({'mode': 'eflp_unique', 'p1_profile': 'p1'}, None, env=env)

    def test_unique_eflp_profiles_come_from_env_in_order(self):
        env = env_for(21) | {'EFLP_UNIQUE_P2_PROFILES': ','.join(str(i) for i in range(20, 0, -1))}
        self.assertEqual(configured_mode_profiles('eflp_unique', env),
                         (None, [str(i) for i in range(20, 0, -1)]))

    def test_eflp_series_requires_common_method_and_each_p2_fiat_account_id(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'EFLP_P1_PROFILES': 'p1_3',
                            'MEXC_P1_3_API_KEY': 'gel-key',
                            'MEXC_P1_3_SECRET_KEY': 'gel-secret',
                            'MEXC_P1_3_MEMBER_ID': 'gel-member',
                            'MEXC_P1_3_NICKNAME': 'GEL maker',
                            'MEXC_P1_3_SELL_ADV_NO': 'a1234567890123456789',
                            'MEXC_P1_3_BUY_ADV_NO': 'a1234567890123456788',
                            'MEXC_P1_3_ADSPOWER_PROFILE_ID': 'gel-browser',
                            'MEXC_P1_3_FIAT': 'GEL'}
        del env['EFLP_PAY_METHOD_ID_GEL']
        with self.assertRaisesRegex(ValueError, 'EFLP_PAY_METHOD_ID_GEL'):
            begin_mode(self.journal, 'eflp_volume', p1_profile='p1_3',
                       p2_profiles=['1'], env=env)
        env['EFLP_PAY_METHOD_ID_GEL'] = '519'
        del env['MEXC_P2_1_ADSPOWER_PROFILE_ID']
        with self.assertRaisesRegex(ValueError, 'MEXC_P2_1_PAYMENT_ID_GEL'):
            begin_mode(self.journal, 'eflp_volume', p1_profile='p1_3',
                       p2_profiles=['1'], env=env)
        env['MEXC_P2_1_PAYMENT_ID_GEL'] = '2253483'
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1_3',
                           p2_profiles=['1'], env=env)
        self.assertEqual(state['p1_profile'], 'p1_3')

    def test_unique_eflp_accepts_fiat_account_ids_without_p2_browsers(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(20) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        for number in range(1, 21):
            del env[f'MEXC_P2_{number}_ADSPOWER_PROFILE_ID']
        state = begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                           p2_profiles=[str(number) for number in range(1, 21)], env=env)
        self.assertEqual(len(state['profiles']), 20)

    def test_eflp_volume_skips_p2_that_is_selected_p1_account(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(3) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        env['MEXC_P2_1_MEMBER_ID'] = env['MEXC_P1_MEMBER_ID']
        env['MEXC_P2_3_API_KEY'] = env['MEXC_P1_API_KEY']
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                           p2_profiles=['1', '2', '3'], env=env)
        self.assertEqual(state['profiles'], ['2'])
        self.assertEqual(next_profile(state)[0], '2')

    def test_eflp_unique_skips_selected_p1_with_small_pool(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(2) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        env['MEXC_P2_1_MEMBER_ID'] = env['MEXC_P1_MEMBER_ID']
        with self.assertRaisesRegex(ValueError, 'осталось 0'):
            begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                       p2_profiles=['1'], env=env)
        state = begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                           p2_profiles=['1', '2'], env=env)
        self.assertEqual(state['profiles'], ['2'])

    async def test_saved_eflp_series_skips_new_alias_before_next_order(self):
        from rollover import save_state

        self.journal.abandon(self.cycle_id)
        env = env_for(3) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                           p2_profiles=['1', '2', '3'], env=env)
        state['cursor'] = 2
        state['eflp_done'] = ['2', '3']
        save_state(self.journal, state)
        env['MEXC_P2_1_MEMBER_ID'] = env['MEXC_P1_MEMBER_ID']
        with patch.dict(os.environ, env):
            with patch('trade_modes.choose_mode_amount', new_callable=AsyncMock,
                       side_effect=Paused('Тест остановил новый ордер')):
                with self.assertRaisesRegex(Paused, 'Тест остановил'):
                    await run_mode(self.journal, state, asyncio.Event(), None)
        self.assertEqual(state['profiles'], ['2', '3'])
        self.assertEqual(state['cursor'], 1)
        self.assertEqual(state['status'], 'paused')

    async def test_saved_eflp_active_cycle_is_not_skipped_without_reconciliation(self):
        from rollover import save_state

        self.journal.abandon(self.cycle_id)
        env = env_for(2) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                           p2_profiles=['1', '2'], env=env)
        active = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_volume',
                                      'p1_profile': 'p1', 'p2_profile': '1'})
        state['active_cycle'] = active
        save_state(self.journal, state)
        env['MEXC_P2_1_MEMBER_ID'] = env['MEXC_P1_MEMBER_ID']
        with patch.dict(os.environ, env):
            with patch('trade_modes.run_command', new_callable=AsyncMock) as trade:
                with self.assertRaisesRegex(Paused, 'Активный цикл Eflp'):
                    await run_mode(self.journal, state, asyncio.Event(), None)
        trade.assert_not_awaited()
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['active_cycle'], active)

    def test_cash_mode_uses_ordinary_return_without_p2_maker_or_deposit(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        del env['MEXC_P2_1_SELL_ADV_NO']
        del env['MEXC_P2_1_ADSPOWER_PROFILE_ID']
        del env['MEXC_P2_1_PAYMENT_ID']
        with self.assertRaisesRegex(ValueError, 'MEXC_P2_1_PAYMENT_ID'):
            begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                       p2_profiles=['1'], env=env)
        env['MEXC_P2_1_PAYMENT_ID'] = '1001'
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        self.assertEqual(state['profiles'], ['1'])
        self.assertEqual(state['cash_policy'], 'rolling24_p1')

    async def test_cash_return_route_is_selected_once_from_confirmed_first_leg(self):
        from rollover import load_state, save_state
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env_for(1) | {
                               'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'})
        anchor = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        state['volume_windows']['1'] = {'quantity': '68500', 'orders': [],
                                         'third_order_at': anchor}
        save_state(self.journal, state)
        spec = dict(self.spec, automatic=True, scheduler_mode='cash_volume', cash_final_return='p2p',
                    p1_profile='p1', p2_profile='1', cash_route_selected=False,
                    cash_p1_buy_adv_no='AD-P1-BUY', cash_p2_sell_adv_no='AD-P2-SELL')
        cid = self.journal.create(spec)
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '1800'},
                                context={'amount': '180000', 'quantity': '1800'})
        runner = fixtures.AutoTests.runner(self)
        runner.cycle_id, runner.spec, runner.p2_profile, runner.p1_profile = cid, spec, '1', 'p1'
        self.assertTrue(runner.select_cash_reverse_route())
        saved = self.journal.cycle(cid)['spec']
        self.assertEqual(saved['reverse_maker'], 'p2')
        self.assertEqual(saved['reverse_adv_no'], 'AD-P2-SELL')
        self.assertEqual(saved['cash_third_order_at'], anchor)
        self.assertFalse(runner.select_cash_reverse_route())
        self.assertEqual(load_state(self.journal)['volume_windows']['1']['quantity'], '70300')

    async def test_new_cash_route_returns_to_p1_before_switching_p2(self):
        from rollover import save_state
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1', '2'], env=env_for(2) | {
                               'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'})
        previous = self.journal.create(dict(self.spec, p2_profile='1',
                                            members={'p1': 'main-member', 'p2': 'member-1'}))
        self.journal.transition(previous, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '67000'},
                                context={'amount': '6700000', 'quantity': '67000'})
        self.journal.abandon(previous)
        spec = dict(self.spec, automatic=True, scheduler_mode='cash_volume',
                    cash_policy='rolling24_p1', p1_profile='p1', p2_profile='1',
                    members={'p1': 'main-member', 'p2': 'member-1'},
                    cash_route_selected=False, cash_p1_buy_adv_no='AD-P1-BUY')
        cid = self.journal.create(spec)
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '1000'},
                                context={'amount': '100000', 'quantity': '1000'})
        runner = fixtures.AutoTests.runner(self)
        runner.cycle_id, runner.spec, runner.p2_profile, runner.p1_profile = cid, spec, '1', 'p1'
        runner.trusted_members = {'p1': 'main-member', 'p2': 'member-1'}
        self.assertTrue(runner.select_cash_reverse_route())
        saved = self.journal.cycle(cid)['spec']
        self.assertEqual(saved['reverse_maker'], 'p1')
        self.assertEqual(saved['reverse_adv_no'], 'AD-P1-BUY')
        self.assertTrue(saved['buy_replenish'])
        self.assertIn('reverse_create', [step.key for step in steps_for_spec(saved)])
        self.journal.transition(cid, 'reverse_replenish_buy', 'p1', 'done', 'refilled')
        self.complete(cid, quantity='1000')
        state['active_cycle'] = cid
        save_state(self.journal, state)
        _finish_completed_cycle(self.journal, state, cid, '1')
        self.assertEqual(state['cursor'], 1)
        self.assertEqual(state['cooldowns']['1']['reason'], 'volume_rolling')
        self.assertEqual(next_profile(state, journal=self.journal)[0], '2')

    async def test_cash_network_route_skips_reverse_order_and_survives_resume(self):
        from rollover import save_state
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        anchor = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        state['volume_windows']['1'] = {'quantity': '69000', 'orders': [],
                                         'third_order_at': anchor}
        save_state(self.journal, state)
        spec = dict(self.spec, automatic=True, scheduler_mode='cash_volume',
                    p1_profile='p1', p2_profile='1', cash_final_return='network',
                    cash_route_selected=False, cash_p1_buy_adv_no='AD-P1-BUY',
                    cash_p2_sell_adv_no='')
        cid = self.journal.create(spec)
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '1600'},
                                context={'amount': '160000', 'quantity': '1600'})
        runner = fixtures.AutoTests.runner(self)
        runner.cycle_id, runner.spec, runner.p2_profile, runner.p1_profile = cid, spec, '1', 'p1'
        self.assertTrue(runner.select_cash_reverse_route())
        saved = self.journal.cycle(cid)['spec']
        self.assertEqual(saved['cash_return_route'], 'network')
        self.assertEqual(saved['reverse_maker'], 'p1')
        self.assertNotIn('reverse_create', [step.key for step in steps_for_spec(saved)])
        self.assertEqual(state['completed_count'], 0)
        with self.assertRaisesRegex(Paused, 'вывод через сеть'):
            await runner.run(cid)
        self.assertIsNone(self.journal.step(cid, 'reverse_create'))

    async def test_cash_network_return_switches_only_after_confirmed_credit(self):
        from rollover import save_state
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        anchor = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        cid = self.journal.create(dict(self.spec, scheduler_mode='cash_volume',
            p1_profile='p1', p2_profile='1', cash_route_selected=True,
            cash_return_route='network', cash_third_order_at=anchor))
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
            result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
        state['active_cycle'] = cid
        save_state(self.journal, state)
        with patch('return_funds.finish_return', new=AsyncMock(side_effect=Paused('Вывод ожидает сверки'))):
            with self.assertRaisesRegex(Paused, 'ожидает сверки'):
                await _finish_cash_network_return(self.journal, state, cid, '1', asyncio.Event())
        self.assertEqual(state['active_cycle'], cid)
        self.assertEqual(state['cursor'], 0)
        self.assertNotEqual(self.journal.cycle(cid)['status'], 'completed')

        async def confirmed(journal, saved, stop):
            saved['pending_return'].update(stage='done', credited='1799.99',
                                           network='BSC', tx_id='confirmed-tx')
            save_state(journal, saved)
        with patch('return_funds.finish_return', side_effect=confirmed) as withdraw:
            await _finish_cash_network_return(self.journal, state, cid, '1', asyncio.Event())
        withdraw.assert_awaited_once()
        self.assertEqual(self.journal.cycle(cid)['status'], 'completed')
        self.assertEqual(state['completed_count'], 1)
        self.assertIsNone(state['active_cycle'])
        self.assertIsNone(state['pending_return'])
        self.assertEqual(state['cooldowns']['1']['anchor'], anchor)

    def test_eflp_pauses_cover_both_orders_and_cash_only_final_return(self):
        for mode in ('eflp_volume', 'eflp_unique'):
            eflp = [step.key for step in steps_for_spec({'scheduler_mode': mode})]
            for key in ('forward_wait_paid', 'forward_wait_release',
                        'reverse_wait_paid', 'reverse_wait_release'):
                self.assertIn(key, eflp)
            self.assertNotIn('forward_check', eflp)
            self.assertNotIn('reverse_check', eflp)
        cash = [step.key for step in steps_for_spec({
            'scheduler_mode': 'cash_volume', 'reverse_maker': 'p2'})]
        self.assertNotIn('forward_wait_paid', cash)
        self.assertIn('reverse_wait_paid', cash)
        self.assertIn('reverse_wait_release', cash)

    async def test_eflp_unique_stops_after_twenty_completed_profiles(self):
        state = {'mode': 'eflp_unique', 'unique_done': [str(i) for i in range(20)]}
        self.assertTrue(_eflp_target_reached(state))

    async def test_eflp_unique_stops_after_all_selected_when_fewer_than_twenty(self):
        state = {'mode': 'eflp_unique', 'unique_done': ['1', '2']}
        self.assertFalse(_eflp_target_reached(state))

    async def test_eflp_unique_keeps_twenty_cap_for_larger_pool(self):
        state = {'mode': 'eflp_volume', 'unique_done': [str(i) for i in range(20)],
                 'eflp_volume_by_profile': {'1': '19999.9999'}}
        self.assertFalse(_eflp_target_reached(state))
        state['eflp_volume_by_profile']['2'] = '0.0001'
        self.assertTrue(_eflp_target_reached(state))

    def test_new_eflp_mode_restores_weekly_volume_and_completed_unique_profiles(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(3) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}

        def sale(profile, quantity, mode, *, complete=True, p1='p1'):
            cid = self.journal.create({'mode': 'api', 'scheduler_mode': mode,
                'p1_profile': p1, 'p2_profile': profile,
                'members': {'p2': env[f'MEXC_P2_{profile}_MEMBER_ID']}})
            self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
                result={'quantity': quantity}, context={'amount': '1000', 'quantity': quantity})
            if complete:
                self.complete(cid, quantity)
            else:
                self.journal.abandon(cid)
            return cid

        first = sale('1', '800', 'eflp_volume')
        sale('1', '100', 'eflp_unique')
        sale('2', '75', 'eflp_unique', complete=False)
        sale('1', '600', 'cash_volume')
        sale('3', '700', 'eflp_volume', p1='p1_2')
        old = sale('1', '1200', 'eflp_volume')
        self.journal.db.execute("UPDATE sales SET completed_at=? WHERE cycle_id=?",
            ((datetime.now(timezone.utc) - timedelta(days=14)).isoformat(), old))
        self.journal.db.commit()

        volume = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                            p2_profiles=['1', '2', '3'], env=env)
        self.assertEqual(volume['eflp_volume_by_profile'], {'1': '900', '2': '75'})
        self.assertEqual(volume['unique_done'], ['1'])
        self.assertEqual(volume['completed_count'], 1)
        self.assertEqual(volume['eflp_counted_cycles'].count(first), 1)
        _record_forward(self.journal, volume, first)
        self.assertEqual(volume['eflp_volume_by_profile']['1'], '900')

        from rollover import save_state
        volume['status'] = 'stopped'
        save_state(self.journal, volume)
        unique = begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                            p2_profiles=['1', '2', '3'], env=env)
        self.assertEqual(unique['unique_done'], ['1'])
        self.assertEqual(unique['completed_count'], 1)
        self.assertEqual(next_profile(unique)[0], '2')

    async def test_sheet_progress_is_restored_before_new_eflp_order(self):
        from rollover import save_state

        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        cid = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_volume',
                                   'p1_profile': 'p1', 'p2_profile': '1',
                                   'members': {'p2': env['MEXC_P2_1_MEMBER_ID']}})
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
            result={'quantity': '20050'}, context={'amount': '100000', 'quantity': '20050'})
        self.complete(cid, '20050')
        state['active_cycle'] = cid
        save_state(self.journal, state)
        sheets = type('Sheets', (), {'read_eflp_sales': AsyncMock(return_value=self.journal.sales())})()
        with patch.dict(os.environ, env), patch('trade_modes.run_command', new_callable=AsyncMock) as trade:
            with self.assertRaisesRegex(Paused, 'Нет доступного П2'):
                await run_mode(self.journal, state, asyncio.Event(), None, sheets=sheets)
        sheets.read_eflp_sales.assert_awaited_once_with(self.journal)
        trade.assert_not_awaited()
        self.assertEqual(state['eflp_volume_by_profile']['1'], '20050')
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['status'], 'paused')

    async def test_sheet_failure_does_not_block_return_of_existing_eflp_cycle(self):
        from rollover import save_state
        from sheets import GoogleSheetsError

        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        cid = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_unique',
            'p1_profile': 'p1', 'p2_profile': '1',
            'members': {'p2': env['MEXC_P2_1_MEMBER_ID']}})
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
            result={'quantity': '100'}, context={'amount': '1000', 'quantity': '100'})
        self.complete(cid, '100')
        state['active_cycle'] = cid
        save_state(self.journal, state)
        sheets = type('Sheets', (), {'read_eflp_sales': AsyncMock(
            side_effect=GoogleSheetsError('нет доступа'))})()
        with patch.dict(os.environ, env), patch('trade_modes.run_command', new_callable=AsyncMock) as trade:
            with self.assertRaisesRegex(Paused, 'нет доступа'):
                await run_mode(self.journal, state, asyncio.Event(), None, sheets=sheets)
        trade.assert_not_awaited()
        self.assertIsNone(state['active_cycle'])
        self.assertEqual(state['completed_count'], 1)

    def test_eflp_volume_waits_for_return_when_unfinished_sale_crosses_target(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        completed = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_volume',
            'p1_profile': 'p1', 'p2_profile': '1',
            'members': {'p2': env['MEXC_P2_1_MEMBER_ID']}})
        self.journal.transition(completed, 'forward_complete', 'both', 'done', 'sold',
            result={'quantity': '19950'}, context={'amount': '1000', 'quantity': '19950'})
        self.complete(completed, '19950')
        pending = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_volume',
            'p1_profile': 'p1', 'p2_profile': '1',
            'members': {'p2': env['MEXC_P2_1_MEMBER_ID']}})
        self.journal.transition(pending, 'forward_complete', 'both', 'done', 'sold',
            result={'quantity': '100'}, context={'amount': '1000', 'quantity': '100'})
        state = {'mode': 'eflp_volume', 'p1_profile': 'p1', 'profiles': ['1']}
        restore_eflp_progress(self.journal, state, self.journal.sales(), env=env)
        self.assertEqual(state['eflp_volume_by_profile']['1'], '20050')
        self.assertEqual(state['unique_done'], ['1'])
        self.assertEqual(state['eflp_done'], [])

    def test_eflp_cycle_from_previous_week_does_not_fill_new_week(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        cid = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_volume',
            'p1_profile': 'p1', 'p2_profile': '1',
            'members': {'p2': env['MEXC_P2_1_MEMBER_ID']}})
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
            result={'quantity': '20050'}, context={'amount': '1000', 'quantity': '20050'})
        self.complete(cid, '20050')
        self.journal.db.execute('UPDATE sales SET completed_at=? WHERE cycle_id=?',
            ((datetime.now(timezone.utc) - timedelta(days=14)).isoformat(), cid))
        self.journal.db.commit()
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        _finish_completed_cycle(self.journal, state, cid, '1')
        self.assertEqual(state['completed_count'], 0)
        self.assertEqual(state['eflp_volume_by_profile'], {})
        self.assertEqual(state['eflp_done'], [])

    async def test_eflp_volume_notifies_when_each_p2_reaches_target(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'eflp_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env)
        cid = self.journal.create({'mode': 'api', 'scheduler_mode': 'eflp_volume',
                                   'p1_profile': 'p1', 'p2_profile': '1', 'forward_only': True})
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
                                result={'quantity': '100'}, context={'amount': '1000', 'quantity': '100'})
        self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
        state['active_cycle'] = cid
        state['terminal_pending'] = {'kind': 'eflp_last', 'from': 'p1', 'to': None,
                                     'p2': '1', 'amount': '1000 RUB', 'quantity': '100'}
        with patch.dict(os.environ, env):
            _finish_terminal_cycle(self.journal, state, cid, '1')
        event = self.journal.step(cid, 'eflp_profile_done')
        self.assertEqual(event['status'], 'done')
        self.assertEqual(state['terminal_pending']['verified'], True)
        notice = next(row for row in self.journal.pending('telegram')
                      if row['step'] == 'eflp_profile_done')
        self.assertIn('Объём Eflp', notice['message'])

    async def test_eflp_final_notice_survives_failed_send_and_restart(self):
        from sheets import Reporter
        cid = self.cycle_id
        self.journal.transition(cid, 'eflp_profile_done', 'system', 'done',
                                '✅ Уникальные Eflp: П1 Maker завершён.')
        telegram = type('Telegram', (), {'enabled': True,
            'send': AsyncMock(side_effect=[False, True])})()
        reporter = Reporter(self.journal, telegram, None)
        await reporter.flush()
        self.assertEqual(len([row for row in self.journal.pending('telegram')
                              if row['step'] == 'eflp_profile_done']), 1)
        await reporter.flush()
        self.assertEqual(telegram.send.await_count, 2)
        self.assertFalse(any(row['step'] == 'eflp_profile_done'
                             for row in self.journal.pending('telegram')))

    async def test_unique_requires_twenty_and_remembers_selected_roles(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(21)
        with self.assertRaisesRegex(ValueError, 'не менее 20'):
            begin_mode(self.journal, 'unique', p1_profile='1', p2_profiles=['2'], env=env)
        state = begin_mode(self.journal, 'unique', p1_profile='1',
                           p2_profiles=[str(i) for i in range(2, 22)], env=env)
        self.assertEqual(state['p1_profile'], '1')
        self.assertEqual(len(state['profiles']), 20)

    async def test_volume_timer_is_third_first_sale_plus_24_hours(self):
        state = {'mode': 'volume', 'profiles': ['1', '2'], 'cursor': 0,
                 'cooldowns': {}, 'volume_windows': {}}
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for index in range(3):
            record_purchase(state, '1', f'c{index}', '24000', start + timedelta(minutes=index))
        name, _ = next_profile(state, start + timedelta(hours=1))
        self.assertEqual(name, '2')
        self.assertEqual(datetime.fromisoformat(state['cooldowns']['1']['until']),
                         start + timedelta(days=1, minutes=2))
        name, _ = next_profile(state, start + timedelta(days=1, minutes=3))
        self.assertEqual(name, '1')
        self.assertEqual(state['volume_windows']['1']['quantity'], '0')

    async def test_timer_uses_first_order_creation_after_confirmed_sale(self):
        from rollover import save_state
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(1))
        cid = self.journal.create(dict(self.spec, p2_profile='1'))
        created = datetime(2026, 10, 1, 1, tzinfo=timezone.utc)
        completed = created + timedelta(minutes=8)
        with patch('journal.now', side_effect=[created.isoformat(), completed.isoformat()]):
            self.journal.transition(cid, 'forward_create', 'p2', 'done', 'created',
                                    result={'order_no': 'd1234567890123456789'})
            self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
                result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
        _record_forward(self.journal, state, cid)
        self.assertEqual(datetime.fromisoformat(state['volume_windows']['1']['orders'][0]['at']), created)

    async def test_amount_rechecks_live_ads_and_does_not_shrink_to_71k_remainder(self):
        env = env_for(1)
        env['MEXC_P1_NICKNAME'] = 'main'
        first = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL',
                 'coinName': 'USDT', 'fiatUnit': 'RUB', 'advStatus': 'OPEN',
                 'price': '100', 'maxSingleTransAmount': '200000',
                 'minSingleTransAmount': '1000', 'availableQuantity': '3000'}
        second = dict(first, advNo=env['MEXC_P2_1_SELL_ADV_NO'],
                      maxSingleTransAmount='200000')
        clients = []
        def factory(*_, **__):
            client = type('Client', (), {})()
            client.get_ad = AsyncMock(return_value=first if len(clients) % 2 == 0 else second)
            client.close = AsyncMock()
            clients.append(client)
            return client
        state = {'mode': 'volume', 'volume_windows': {'1': {
            'quantity': '70000', 'orders': [], 'third_order_at': None}}}
        with patch.dict(os.environ, env), patch('trade_modes.MexcP2PClient', side_effect=factory):
            self.assertIsNone(await choose_mode_amount('volume', 'p1', '1', state, env))
            unique = await choose_mode_amount('unique', 'p1', '1', state, env)
        self.assertGreaterEqual(Decimal(unique.split()[0]), Decimal('5000'))
        self.assertLessEqual(Decimal(unique.split()[0]), Decimal('10000'))
        self.assertEqual(len(clients), 4)
        self.assertTrue(all(client.close.await_count == 1 for client in clients))

    async def test_eflp_volume_has_no_200_usdt_minimum_and_uses_100_to_150_offset(self):
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        sell = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL',
                'coinName': 'USDT', 'fiatUnit': 'RUB', 'advStatus': 'OPEN',
                'price': '100', 'maxSingleTransAmount': '18000',
                'minSingleTransAmount': '1000', 'availableQuantity': '180'}
        buy = dict(sell, advNo=env['MEXC_P1_BUY_ADV_NO'], side='BUY')
        client = type('Client', (), {})()
        client.get_ad = AsyncMock(side_effect=lambda ad_no: sell if ad_no == sell['advNo'] else buy)
        client.close = AsyncMock()
        with (patch.dict(os.environ, env),
              patch('trade_modes.MexcP2PClient', return_value=client),
              patch('trade_modes.random.randint', return_value=125) as draw):
            amount = await choose_mode_amount('eflp_volume', 'p1', '1', {}, env)
            quantity = Decimal(amount.split()[0]) / Decimal('100')
            self.assertLess(quantity, 200)
            self.assertGreaterEqual(Decimal('180') - quantity, 100)
            self.assertLessEqual(Decimal('180') - quantity, 150)
            draw.assert_called_once_with(100, 150)
            sell['maxSingleTransAmount'] = buy['maxSingleTransAmount'] = '8000'
            sell['availableQuantity'] = buy['availableQuantity'] = '80'
            amount = await choose_mode_amount('eflp_volume', 'p1', '1', {}, env)
            self.assertGreaterEqual(Decimal(amount.split()[0]), Decimal('1000'))
            self.assertLess(Decimal(amount.split()[0]), Decimal('8000'))
            draw.assert_called_once()

    async def test_eflp_rejects_live_ad_in_different_fiat(self):
        env = env_for(1) | {'MEXC_P1_FIAT': 'GEL',
                            'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        sell = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL',
                'coinName': 'USDT', 'fiatUnit': 'KZT', 'advStatus': 'OPEN'}
        buy = dict(sell, advNo=env['MEXC_P1_BUY_ADV_NO'], side='BUY')
        client = type('Client', (), {})()
        client.get_ad = AsyncMock(side_effect=lambda ad_no: sell if ad_no == sell['advNo'] else buy)
        client.close = AsyncMock()
        with (patch.dict(os.environ, env),
              patch('trade_modes.MexcP2PClient', return_value=client)):
            with self.assertRaisesRegex(Paused, 'MEXC_P1_FIAT'):
                await choose_mode_amount('eflp_volume', 'p1', '1', {}, env)
        client.close.assert_awaited_once()

    async def test_cash_amount_uses_journal_across_runs_and_stops_at_68k(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        previous = self.journal.create(dict(self.spec, p2_profile='1',
                                            members={'p1': 'main-member', 'p2': 'member-1'}))
        self.journal.transition(previous, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '68000'},
                                context={'amount': '6800000', 'quantity': '68000'})
        self.journal.abandon(previous)
        sell = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL',
                'coinName': 'USDT', 'fiatUnit': 'RUB', 'advStatus': 'OPEN',
                'price': '100', 'maxSingleTransAmount': '100000',
                'minSingleTransAmount': '1000', 'availableQuantity': '3000'}
        buy = dict(sell, advNo=env['MEXC_P1_BUY_ADV_NO'], side='BUY')
        client = type('Client', (), {})()
        client.get_ad = AsyncMock(side_effect=lambda ad_no: sell if ad_no == sell['advNo'] else buy)
        client.close = AsyncMock()
        state = {'mode': 'cash_volume', 'volume_windows': {'1': {'quantity': '0', 'orders': []}}}
        with patch.dict(os.environ, env), patch('trade_modes.MexcP2PClient', return_value=client):
            self.assertIsNone(await choose_mode_amount('cash_volume', 'p1', '1', state,
                                                        env, journal=self.journal))
        self.assertEqual(client.close.await_count, 1)

    async def test_cash_preflight_blocks_changed_price_before_order_submission(self):
        self.journal.abandon(self.cycle_id)
        old = self.journal.create(dict(self.spec, p2_profile='1',
                                       members={'p2': 'member-1'}))
        self.journal.transition(old, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '68800'},
                                context={'amount': '6880000', 'quantity': '68800'})
        self.journal.abandon(old)
        runner = fixtures.AutoTests.runner(self)
        runner.cycle_id = self.cycle_id
        runner.p2_profile = '1'
        runner.trusted_members = {'p1': 'main-member', 'p2': 'member-1'}
        runner.spec = dict(self.spec, cash_policy='rolling24_p1', amount='180000')
        self.journal.transition(self.cycle_id, 'forward_ad', 'p1', 'done', 'ad',
                                result={'adv_no': 'AD-SELL'})
        runner.clients['p1'].get_ad = AsyncMock(return_value={
            'advNo': 'AD-SELL', 'side': 'SELL', 'advStatus': 'OPEN', 'price': '100'})
        with self.assertRaises(CashVolumeLimitReached):
            await runner.prepare(Step('forward_create', 'p2', 'create'), recovery=False)
        self.assertIsNone(self.journal.step(self.cycle_id, 'forward_create'))

    async def test_cash_late_limit_moves_to_next_p2_without_submitting_order(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(2) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1', '2'], env=env)
        old = self.journal.create(dict(self.spec, p2_profile='1', members={'p2': 'member-1'}))
        self.journal.transition(old, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '67000'},
                                context={'amount': '6700000', 'quantity': '67000'})
        self.journal.abandon(old)
        state['last_cycle_rowid'] = self.journal.db.execute(
            'SELECT rowid FROM cycles WHERE id=?', (old,)).fetchone()[0]
        seen = []
        async def fake_run(args, **kwargs):
            seen.append(args.p2_profile)
            if len(seen) == 1:
                self.journal.create(dict(self.spec, automatic=True,
                    scheduler_mode='cash_volume', cash_policy='rolling24_p1',
                    p1_profile='p1', p2_profile='1', series={'count': 1}))
                raise CashVolumeLimitReached('limit')
            raise OperatorStopped('stop')
        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')):
            with patch('trade_modes.run_command', side_effect=fake_run):
                with self.assertRaises(OperatorStopped):
                    await run_mode(self.journal, state, asyncio.Event(), None)
        self.assertEqual(seen, ['1', '2'])
        self.assertIsNone(state['active_cycle'])
        self.assertEqual(state['cooldowns']['1']['reason'], 'order_cap')

    async def test_completed_cycle_is_counted_once_before_next_launch(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1)
        state = begin_mode(self.journal, 'volume', env=env)
        stop = asyncio.Event()
        async def complete(args, **_):
            cycle_id = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
                scheduler_mode='volume', p1_profile='p1', p2_profile=args.p2_profile, series={'count': 1}))
            self.journal.transition(cycle_id, 'forward_complete', 'both', 'done', 'first sale',
                result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
            self.complete(cycle_id)
            stop.set()
        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=complete):
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, stop, None)
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['volume_windows']['1']['quantity'], '1800')

    async def test_chat_proxy_reset_waits_and_resumes_same_cycle(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(1))
        stop = asyncio.Event()
        calls = []

        async def trade(args, **_):
            calls.append(args)
            if len(calls) == 1:
                cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
                    scheduler_mode='volume', p1_profile='p1', p2_profile='1', series={'count': 1}))
                self.journal.transition(cid, 'reverse_reply', 'p1', 'unknown', 'chat connection reset',
                                        result={'order_no': 'ORDER-2', 'text': 'Сохранённая фраза'})
                raise MexcChatUnavailable('Chat proxy connection failed (ConnectionResetError)')
            self.assertEqual(args.resume, state['active_cycle'])
            self.journal.transition(args.resume, 'forward_complete', 'both', 'done', 'sale',
                                    result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
            self.complete(args.resume)
            stop.set()

        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=trade), \
                patch('trade_modes.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, stop, None)
        self.assertEqual(len(calls), 2)
        wait.assert_awaited_once()
        self.assertEqual(state['completed_count'], 1)

    async def test_cash_read_timeout_before_new_cycle_retries_without_creating_order(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env_for(1) | {
                               'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'})
        read = AsyncMock(side_effect=[
            MexcReadUnavailable('MEXC read request failed (ConnectTimeout); no action was sent'),
            '180000 RUB',
        ])
        with patch('trade_modes.choose_mode_amount', read), \
                patch('trade_modes.run_command', new_callable=AsyncMock,
                      side_effect=OperatorStopped('stop')) as trade, \
                patch('trade_modes.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, asyncio.Event(), None)
        self.assertEqual(read.await_count, 2)
        trade.assert_awaited_once()
        wait.assert_awaited_once()
        self.assertEqual(len(self.journal.cycles()), 1)
        self.assertNotIn('mexc_read_waiting', state)

    async def test_cash_network_return_read_timeout_retries_same_cycle(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env_for(1) | {
                               'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'})
        cycle_id = self.journal.create(dict(self.spec, automatic=True,
            scheduler_mode='cash_volume', p1_profile='p1', p2_profile='1',
            cash_return_route='network', series={'count': 1}))
        state['active_cycle'] = cycle_id
        with patch('trade_modes._finish_cash_network_return', new_callable=AsyncMock,
                   side_effect=[MexcReadUnavailable('MEXC read request failed (ConnectTimeout)'),
                                OperatorStopped('stop')]) as finish, \
                patch('trade_modes.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, asyncio.Event(), None)
        self.assertEqual(finish.await_count, 2)
        wait.assert_awaited_once()
        self.assertEqual(state['active_cycle'], cycle_id)

    async def test_cash_ads_runtime_timeout_waits_and_resumes_same_cycle(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env_for(1) | {
                               'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'})
        stop = asyncio.Event()
        calls = []

        async def trade(args, **_):
            calls.append(args)
            if len(calls) == 1:
                cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p1',
                    scheduler_mode='cash_volume', p1_profile='p1', p2_profile='1', series={'count': 1}))
                self.journal.transition(cid, 'reverse_replenish_buy', 'p1', 'unknown',
                    'AdsPower Runtime.evaluate timed out', result={'adv_no': 'AD-BUY',
                    'quantity': '100', 'target_available': '120'})
                raise AdsPowerTimeout('AdsPower: команда Runtime.evaluate не ответила за 10 секунд')
            self.assertEqual(args.resume, state['active_cycle'])
            self.complete(args.resume)
            stop.set()

        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=trade), \
                patch('trade_modes.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, stop, None)
        self.assertEqual(len(calls), 2)
        wait.assert_awaited_once()
        self.assertEqual(state['completed_count'], 1)

    async def test_cash_retries_generic_adspower_status_failure_without_operator(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'cash_volume', p1_profile='p1',
                           p2_profiles=['1'], env=env_for(1) | {
                               'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'})
        stop = asyncio.Event()
        calls = []

        async def trade(args, **_):
            calls.append(args)
            if len(calls) == 1:
                raise AdsPowerUnavailable('AdsPower временно отклонил проверку профиля (код -1)')
            cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p1',
                scheduler_mode='cash_volume', p1_profile='p1', p2_profile='1', series={'count': 1}))
            self.complete(cid)
            stop.set()

        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=trade), \
                patch('trade_modes.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, stop, None)
        self.assertEqual(len(calls), 2)
        wait.assert_awaited_once()
        self.assertEqual(state['completed_count'], 1)

    async def test_three_consecutive_explicit_85010_rejections_raise_one_alert(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(1))
        for index in range(3):
            cid = self.journal.create(dict(self.spec, automatic=True, p2_profile='1'))
            self.journal.transition(cid, 'forward_create', 'p2', 'rejected', '85010',
                                    result={'rejected_code': 85010}, cycle_status='paused')
            self.assertTrue(_handle_forward_rejection(self.journal, state, cid, '1'))
            self.assertEqual(self.journal.cycle(cid)['status'], 'abandoned')
        self.assertEqual(state['ad_rejection_streaks']['1'], 3)
        alerts = self.journal.db.execute("SELECT COUNT(*) FROM events WHERE step='ad_rejection_alert'").fetchone()[0]
        self.assertEqual(alerts, 1)

    async def test_restart_accounts_for_completed_cycle_saved_before_scheduler_state(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(1))
        cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
            scheduler_mode='volume', p1_profile='p1', p2_profile='1', series={'count': 1}))
        self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
            result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
        self.complete(cid)
        stop = asyncio.Event()
        def finish(journal, saved_state, cycle_id, profile):
            self.assertEqual(cycle_id, cid)
            _finish_completed_cycle(journal, saved_state, cycle_id, profile)
            stop.set()
        with patch('trade_modes._finish_completed_cycle', side_effect=finish), \
                patch('trade_modes.run_command', new_callable=AsyncMock) as trade:
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, stop, None)
        trade.assert_not_awaited()
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['volume_windows']['1']['quantity'], '1800')

    async def test_reverse_rejection_keeps_cycle_and_does_not_switch_with_funds_on_p2(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(2))
        async def reject(args, **_):
            cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
                scheduler_mode='volume', p1_profile='p1', p2_profile=args.p2_profile,
                series={'count': 1}))
            self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
                result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
            self.journal.transition(cid, 'reverse_create', 'p1', 'rejected', '85010',
                                    result={'rejected_code': 85010}, cycle_status='paused')
        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=reject):
            with self.assertRaisesRegex(Paused, 'не завершён'):
                await run_mode(self.journal, state, asyncio.Event(), None)
        self.assertIsNotNone(state['active_cycle'])
        self.assertEqual(state['volume_windows']['1']['quantity'], '1800')
        self.assertEqual(state['cursor'], 0)

    async def test_volume_switch_notice_is_recorded_after_completed_return(self):
        from rollover import cycle_rowid, save_state
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(2))
        completed_id = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
            scheduler_mode='volume', p1_profile='p1', p2_profile='1', series={'count': 1}))
        self.complete(completed_id)
        state['last_cycle_rowid'] = cycle_rowid(self.journal, completed_id)
        state['last_completed'] = {'cycle_id': completed_id, 'profile': '1', 'quantity': '1800'}
        state['cooldowns']['1'] = {'until': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                                   'manual_block': False, 'reason': 'volume'}
        save_state(self.journal, state)
        stop = asyncio.Event()
        async def trade(args, **_):
            self.assertEqual(args.p2_profile, '2')
            cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
                scheduler_mode='volume', p1_profile='p1', p2_profile='2', series={'count': 1}))
            self.complete(cid)
            stop.set()
        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=trade):
            with self.assertRaises(OperatorStopped):
                await run_mode(self.journal, state, stop, None)
        notice = self.journal.step(completed_id, 'volume_switch_notice')
        self.assertEqual(notice['status'], 'done')

    async def test_first_sale_is_counted_even_if_later_step_raises(self):
        self.journal.abandon(self.cycle_id)
        state = begin_mode(self.journal, 'volume', env=env_for(1))
        async def fail_after_sale(args, **_):
            cid = self.journal.create(dict(self.spec, automatic=True, reverse_maker='p2',
                scheduler_mode='volume', p1_profile='p1', p2_profile='1', series={'count': 1}))
            self.journal.transition(cid, 'forward_complete', 'both', 'done', 'sale',
                result={'quantity': '1800'}, context={'amount': '180000', 'quantity': '1800'})
            raise RuntimeError('reverse unavailable')
        with patch('trade_modes.choose_mode_amount', new=AsyncMock(return_value='180000 RUB')), \
                patch('trade_modes.run_command', side_effect=fail_after_sale):
            with self.assertRaisesRegex(RuntimeError, 'reverse unavailable'):
                await run_mode(self.journal, state, asyncio.Event(), None)
        self.assertEqual(state['volume_windows']['1']['quantity'], '1800')
        self.assertIsNotNone(state['active_cycle'])
