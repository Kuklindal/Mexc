import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch

from cycle import OperatorStopped, Paused
from adspower import AdsPowerUnavailable
from mexc_client import MexcAPIError, MexcReadUnavailable
from rollover import (amount_from_ad, begin, cooldown_after_limit, eligible,
                      enqueue_switch_notice, finish_limited_cycle, load_state, next_wait, run, save_state)
from telegram_control import TelegramControl
from sheets import Reporter
import test_auto as fixtures


class RolloverTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown
    async def test_amount_uses_live_ad_limit_and_price(self):
        ad = {'side': 'SELL', 'coinName': 'USDT', 'price': '88.5',
              'maxSingleTransAmount': '40000', 'minSingleTransAmount': '500',
              'availableQuantity': '1000'}
        low, high, price = amount_from_ad(ad)
        self.assertEqual(price, Decimal('88.5'))
        self.assertEqual(high, Decimal('30265.00'))  # 40000 - 110 * 88.5
        self.assertEqual(low, Decimal('21415.00'))   # high - 100 * 88.5
        ad['maxSingleTransAmount'] = '26000'
        with self.assertRaisesRegex(Paused, '200 USDT'):
            amount_from_ad(ad)

    async def test_cooldown_uses_third_trade_not_limit_time(self):
        self.journal.abandon(self.cycle_id)
        base = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)
        for i in range(4):
            cid = self.journal.create(dict(self.spec, p2_profile='default'))
            with patch('journal.now', return_value=(base + timedelta(hours=i)).isoformat()):
                self.journal.transition(cid, 'forward_create', 'p2', 'done', 'created')
            self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
        limit = cooldown_after_limit(self.journal, 'default', base + timedelta(hours=8))
        self.assertTrue(limit['third_trade'])
        self.assertEqual(datetime.fromisoformat(limit['until']), base + timedelta(hours=26))
        self.assertFalse(limit['manual_block'])

    async def test_yesterday_third_trade_is_used_when_today_has_one(self):
        self.journal.abandon(self.cycle_id)
        base = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)
        for stamp in (base, base + timedelta(hours=1), base + timedelta(hours=2),
                      base + timedelta(days=1)):
            cid = self.journal.create(dict(self.spec, p2_profile='default'))
            with patch('journal.now', return_value=stamp.isoformat()):
                self.journal.transition(cid, 'forward_create', 'p2', 'done', 'created')
            self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
        anchor = cooldown_after_limit(self.journal, 'default', base + timedelta(days=1, hours=1))
        self.assertEqual(datetime.fromisoformat(anchor['anchor']), base + timedelta(hours=2))

    async def test_profile_selection_waits_and_chooses_next(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default,2'}):
            state = begin(self.journal, 'all')
        now_utc = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state['cooldowns']['default'] = {'until': (now_utc + timedelta(hours=1)).isoformat(),
                                         'manual_block': False}
        self.assertEqual(eligible(state, now_utc), '2')
        state['cooldowns']['2'] = {'until': (now_utc + timedelta(hours=2)).isoformat(),
                                   'manual_block': False}
        self.assertIsNone(eligible(state, now_utc))
        self.assertEqual(next_wait(state, now_utc), now_utc + timedelta(hours=1))
        self.assertEqual(eligible(state, now_utc + timedelta(hours=1, seconds=1)), 'default')

    async def test_existing_finite_scheduler_becomes_unlimited(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default'}):
            state = begin(self.journal, 'all')
        state.pop('last_cycle_rowid')
        state.pop('completed_count')
        state.update(target=1, completed_ids=['old-cycle'], known_cycle_ids=[self.cycle_id])
        save_state(self.journal, state)
        stop = asyncio.Event()
        stop.set()
        with self.assertRaises(OperatorStopped):
            await run(self.journal, state, stop, None)
        saved = load_state(self.journal)
        self.assertEqual(saved['completed_count'], 1)
        self.assertNotIn('target', saved)
        self.assertNotIn('completed_ids', saved)

    async def test_scheduler_keeps_profile_until_stopped(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default,2'}):
            state = begin(self.journal, 'all')
        stop = asyncio.Event()
        profiles = []
        async def complete(args, **_):
            profiles.append(args.p2_profile)
            cid = self.journal.create(dict(self.spec, p2_profile=args.p2_profile))
            self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
            if len(profiles) == 3:
                stop.set()
        with patch('rollover.choose_amount', new=AsyncMock(return_value='9000 RUB')) as amounts, \
                patch('rollover.run_command', side_effect=complete):
            with self.assertRaises(OperatorStopped):
                await run(self.journal, state, stop, None)
        self.assertEqual(amounts.await_count, 3)
        self.assertEqual(profiles, ['default'] * 3)
        self.assertEqual(state['completed_count'], 3)
        self.assertEqual(load_state(self.journal)['status'], 'paused')

    async def test_scheduler_waits_and_resumes_after_ads_local_api_timeout(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default'}):
            state = begin(self.journal, 'all')
        stop = asyncio.Event()
        calls = 0
        async def trade(args, **_):
            nonlocal calls
            calls += 1
            if calls == 1:
                cid = self.journal.create(dict(self.spec, automatic=True, p2_profile='default',
                    series={'count': 1}))
                self.journal.transition(cid, 'forward_check', 'p1', 'waiting',
                    'AdsPower read timeout', cycle_status='paused')
                raise AdsPowerUnavailable('Local API ReadTimeout')
            cid = state['active_cycle']
            self.assertEqual(args.resume, cid)
            if calls == 2:
                raise AdsPowerUnavailable('Local API ReadTimeout')
            self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
            stop.set()
        bot = type('Bot', (), {'enabled': True})()
        bot.send = AsyncMock(return_value=True)
        with patch('rollover.choose_amount', new=AsyncMock(return_value='9000 RUB')), \
                patch('rollover.run_command', side_effect=trade), \
                patch('rollover.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run(self.journal, state, stop, bot)
        self.assertEqual(calls, 3)
        self.assertEqual(wait.await_count, 2)
        bot.send.assert_awaited_once()
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['completed_count'], 1)

    async def test_scheduler_retries_read_timeout_on_same_cycle(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default'}):
            state = begin(self.journal, 'all')
        stop = asyncio.Event()
        calls = 0

        async def trade(args, **_):
            nonlocal calls
            calls += 1
            if calls == 1:
                cid = self.journal.create(dict(self.spec, automatic=True, p2_profile='default',
                    series={'count': 1}))
                self.journal.transition(cid, 'forward_release', 'p1', 'waiting',
                    'MEXC read timeout', cycle_status='paused')
                raise MexcReadUnavailable('MEXC read request failed (ReadTimeout); no action was sent')
            self.assertEqual(args.resume, state['active_cycle'])
            self.journal.transition(state['active_cycle'], 'cycle', 'both', 'completed',
                                    'done', cycle_status='completed')
            stop.set()

        with patch('rollover.choose_amount', new=AsyncMock(return_value='9000 RUB')), \
                patch('rollover.run_command', side_effect=trade), \
                patch('rollover.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run(self.journal, state, stop, None)
        self.assertEqual(calls, 2)
        wait.assert_awaited_once()
        self.assertEqual(state['completed_count'], 1)

    async def test_timestamp_rejection_resumes_same_cycle_without_starting_another(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default'}):
            state = begin(self.journal, 'all')
        stop = asyncio.Event()
        calls = []

        async def trade(args, **_):
            calls.append(args)
            if len(calls) == 1:
                cid = self.journal.create(dict(self.spec, automatic=True, p2_profile='default',
                                               series={'count': 1}))
                self.journal.transition(cid, 'forward_create', 'p2', 'rejected', '700003',
                                        result={'rejected_code': 700003}, cycle_status='paused')
                self.journal.transition(cid, 'forward_create', 'p2', 'waiting', 'retry')
                raise MexcAPIError('Timestamp outside recvWindow', code=700003, http_status=400)
            self.assertEqual(args.resume, state['active_cycle'])
            self.journal.transition(args.resume, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
            stop.set()

        with patch('rollover.choose_amount', new=AsyncMock(return_value='9000 RUB')) as amount, \
                patch('rollover.run_command', side_effect=trade), \
                patch('rollover.wait_until', new=AsyncMock()) as wait:
            with self.assertRaises(OperatorStopped):
                await run(self.journal, state, stop, None)
        self.assertEqual(len(calls), 2)
        amount.assert_awaited_once()
        wait.assert_awaited_once()
        self.assertEqual(state['completed_count'], 1)

    async def test_persistent_timestamp_rejection_stops_after_bounded_retries(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default'}):
            state = begin(self.journal, 'all')
        calls = 0

        async def reject(args, **_):
            nonlocal calls
            calls += 1
            if calls == 1:
                cid = self.journal.create(dict(self.spec, automatic=True, p2_profile='default',
                                               series={'count': 1}))
            else:
                cid = args.resume
            self.journal.transition(cid, 'forward_create', 'p2', 'rejected', '700003',
                                    result={'rejected_code': 700003}, cycle_status='paused')
            self.journal.transition(cid, 'forward_create', 'p2', 'waiting', 'retry')
            raise MexcAPIError('Timestamp outside recvWindow', code=700003, http_status=400)

        with patch('rollover.choose_amount', new=AsyncMock(return_value='9000 RUB')) as amount, \
                patch('rollover.run_command', side_effect=reject), \
                patch('rollover.wait_until', new=AsyncMock()) as wait:
            with self.assertRaisesRegex(Paused, '700003'):
                await run(self.journal, state, asyncio.Event(), None)
        self.assertEqual(calls, 4)
        self.assertEqual(wait.await_count, 3)
        amount.assert_awaited_once()
        self.assertEqual(load_state(self.journal)['status'], 'paused')

    async def test_limit_on_reverse_returns_then_uses_next_profile(self):
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default,2'}):
            state = begin(self.journal, 'all')
        stop = asyncio.Event()
        first = []
        profiles = []
        async def trade(args, **_):
            profiles.append(args.p2_profile)
            cid = self.journal.create(dict(self.spec, automatic=True, p2_profile=args.p2_profile,
                series={'count': 1}))
            if args.p2_profile == 'default':
                first.append(cid)
                self.journal.transition(cid, 'forward_create', 'p2', 'done', 'created')
                self.journal.transition(cid, 'forward_complete', 'both', 'done', 'done',
                    result={'quantity': '100'}, context={'amount': '9000', 'fiat': 'RUB', 'quantity': '100'})
                self.journal.transition(cid, 'reverse_create', 'p2', 'rejected', '60085',
                                        result={'rejected_code': 60085}, cycle_status='paused')
            else:
                self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
                stop.set()
        async def returned(_, scheduler, __):
            self.assertEqual(scheduler['pending_return']['profile'], 'default')
            scheduler['pending_return']['stage'] = 'done'
            scheduler['pending_return']['credited'] = '99.99'
            save_state(self.journal, scheduler)
        with patch('rollover.choose_amount', new=AsyncMock(return_value='9000 RUB')), \
                patch('rollover.run_command', side_effect=trade), \
                patch('return_funds.finish_return', side_effect=returned):
            with self.assertRaises(OperatorStopped):
                await run(self.journal, state, stop, None)
        self.assertEqual(profiles, ['default', '2'])
        self.assertEqual(self.journal.cycle(first[0])['status'], 'abandoned')
        self.assertEqual(state['completed_count'], 1)
        self.assertIn('default', state['cooldowns'])
        notice = self.journal.step(first[0], 'rollover_switch_notice')
        self.assertEqual(notice['result']['next_profile'], '2')
        bot = type('Bot', (), {'enabled': True})()
        bot.send = AsyncMock(return_value=True)
        reporter = Reporter(self.journal, bot, None)
        await reporter.flush()
        sent = [call.args[0] for call in bot.send.await_args_list]
        self.assertEqual(len(sent), 1)
        self.assertIn('99.99 USDT', sent[0])
        self.assertIn('Следующий П2:', sent[0])
        await reporter.flush()
        self.assertEqual(bot.send.await_count, 1)

    async def test_single_profile_reports_return_and_notice_survives_recovery(self):
        state = {'mode': 'single', 'profiles': ['default'], 'cursor': 0,
                 'active_cycle': self.cycle_id,
                 'pending_return': {'cycle_id': self.cycle_id, 'profile': 'default',
                                    'stage': 'done', 'credited': '100.0000'}}
        finish_limited_cycle(self.journal, state)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'abandoned')
        self.assertIsNone(state['pending_return'])
        self.assertNotIn('pending_switch_notice', state)
        self.assertEqual(self.journal.step(self.cycle_id, 'rollover_switch_notice')['result']['next_profile'],
                         'default')
        count = self.journal.db.execute("SELECT COUNT(*) FROM events WHERE cycle_id=? AND step='rollover_switch_notice'",
                                        (self.cycle_id,)).fetchone()[0]
        state['pending_switch_notice'] = {'cycle_id': self.cycle_id, 'profile': 'default',
                                           'from_name': 'P2', 'credited': '100.0000'}
        enqueue_switch_notice(self.journal, state, 'default')
        self.assertEqual(self.journal.db.execute(
            "SELECT COUNT(*) FROM events WHERE cycle_id=? AND step='rollover_switch_notice'",
            (self.cycle_id,)).fetchone()[0], count)

    async def test_bot_rejects_other_user_profile_buttons(self):
        self.journal.abandon(self.cycle_id)
        bot = type('Bot', (), {'bot_token': 'fake', 'chat_id': '123'})()
        bot.send = AsyncMock(return_value=True)
        bot.request = AsyncMock(return_value=True)
        ctl = TelegramControl(self.journal, bot, 123, 'default')
        callback = {'update_id': 1, 'callback_query': {'id': '1', 'from': {'id': 999},
                    'data': ctl.nonce + ':single:2', 'message': {'chat': {'id': 123}}}}
        with patch.object(ctl, 'work_rollover', new=AsyncMock()) as work:
            await ctl.handle(callback)
        work.assert_not_called()
        self.assertIsNone(load_state(self.journal))

    async def test_bot_starts_all_and_changes_next_profile(self):
        self.journal.abandon(self.cycle_id)
        bot = type('Bot', (), {'bot_token': 'fake', 'chat_id': '123'})()
        bot.send = AsyncMock(return_value=True)
        bot.request = AsyncMock(return_value=True)
        ctl = TelegramControl(self.journal, bot, 123, 'default')
        env = {'ROLLOVER_PROFILES': 'default,2',
               'MEXC_P1_MEMBER_ID': 'P1',
               'MEXC_P2_API_KEY': 'KEY1', 'MEXC_P2_SECRET_KEY': 'S1',
               'MEXC_P2_MEMBER_ID': 'P2-1', 'MEXC_P2_NICKNAME': 'N1', 'MEXC_P2_PAYMENT_ID': '1',
               'MEXC_P2_2_API_KEY': 'KEY2', 'MEXC_P2_2_SECRET_KEY': 'S2',
               'MEXC_P2_2_MEMBER_ID': 'P2-2', 'MEXC_P2_2_NICKNAME': 'N2', 'MEXC_P2_2_PAYMENT_ID': '2'}
        def callback(action, update_id):
            return {'update_id': update_id, 'callback_query': {'id': str(update_id),
                'from': {'id': 123}, 'data': ctl.nonce + ':' + action,
                'message': {'chat': {'id': 123}}}}
        with patch.dict(os.environ, env), patch.object(ctl, 'work_rollover', new=AsyncMock()) as work:
            await ctl.handle(callback('all', 1))
            await ctl.task
            work.assert_awaited_once()
            self.assertEqual(load_state(self.journal)['mode'], 'all')
            await ctl.handle(callback('select:2', 2))
            self.assertEqual(load_state(self.journal)['cursor'], 1)
