import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch

from cycle import CashVolumeLimitReached, OperatorStopped, Paused, Step, steps_for_spec
from trade_modes import (_finish_cash_network_return, _finish_completed_cycle,
                          _handle_forward_rejection, _record_forward,
                          begin_mode, choose_mode_amount, configured_mode_profiles,
                          next_profile, run_mode)
from volume_policy import record_purchase
import test_auto as fixtures


def env_for(count=20):
    env = {
        'ROLLOVER_PROFILES': ','.join(str(i) for i in range(1, count + 1)),
        'MEXC_P1_API_KEY': 'main-key', 'MEXC_P1_SECRET_KEY': 'main-secret',
        'MEXC_P1_MEMBER_ID': 'main-member', 'MEXC_P1_SELL_ADV_NO': 'a1234567890123456789',
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
                            'EFLP_VOLUME_P2_PROFILES': '2,3'}
        self.assertEqual(configured_mode_profiles('cash_volume', env), ('p1', ['3', '1']))
        self.assertEqual(configured_mode_profiles('eflp_volume', env), (None, ['2', '3']))
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

    def test_cash_mode_uses_ordinary_return_without_p2_maker_or_deposit(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        del env['MEXC_P2_1_SELL_ADV_NO']
        del env['MEXC_P2_1_ADSPOWER_PROFILE_ID']
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
                                result={'quantity': '68000'},
                                context={'amount': '6800000', 'quantity': '68000'})
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
        eflp = [step.key for step in steps_for_spec({'scheduler_mode': 'eflp_volume'})]
        for key in ('forward_wait_paid', 'forward_wait_release',
                    'reverse_wait_paid', 'reverse_wait_release'):
            self.assertIn(key, eflp)
        cash = [step.key for step in steps_for_spec({
            'scheduler_mode': 'cash_volume', 'reverse_maker': 'p2'})]
        self.assertNotIn('forward_wait_paid', cash)
        self.assertIn('reverse_wait_paid', cash)
        self.assertIn('reverse_wait_release', cash)

    async def test_eflp_unique_stops_after_twenty_completed_profiles(self):
        from rollover import save_state
        self.journal.abandon(self.cycle_id)
        env = env_for(20) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        env.update({f'MEXC_P2_{index}_PAYMENT_ID': str(1000 + index)
                    for index in range(1, 21)})
        state = begin_mode(self.journal, 'eflp_unique', p1_profile='p1',
                           p2_profiles=[str(index) for index in range(1, 21)], env=env)
        state['unique_done'] = list(state['profiles'])
        save_state(self.journal, state)
        with patch('trade_modes.run_command', new_callable=AsyncMock) as trade:
            await run_mode(self.journal, state, asyncio.Event(), None)
        trade.assert_not_awaited()
        self.assertEqual(state['status'], 'done')

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

    async def test_cash_amount_uses_journal_across_runs_and_stops_before_70k(self):
        self.journal.abandon(self.cycle_id)
        env = env_for(1) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788'}
        previous = self.journal.create(dict(self.spec, p2_profile='1',
                                            members={'p1': 'main-member', 'p2': 'member-1'}))
        self.journal.transition(previous, 'forward_complete', 'both', 'done', 'sale',
                                result={'quantity': '68800'},
                                context={'amount': '6880000', 'quantity': '68800'})
        self.journal.abandon(previous)
        sell = {'advNo': env['MEXC_P1_SELL_ADV_NO'], 'side': 'SELL',
                'coinName': 'USDT', 'fiatUnit': 'RUB', 'advStatus': 'OPEN',
                'price': '100', 'maxSingleTransAmount': '200000',
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
                                result={'quantity': '68800'},
                                context={'amount': '6880000', 'quantity': '68800'})
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
