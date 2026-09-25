import asyncio
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from cycle import OperatorStopped
from telegram_control import TelegramControl, daily_stats
from notifier import TelegramNotifier
import test_auto as fixtures


class ControlTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown
    runner = fixtures.AutoTests.runner

    def control(self):
        telegram = type('Bot', (), {'bot_token': 'fake-token', 'chat_id': '123', 'enabled': True})()
        telegram.send = AsyncMock(return_value=True)
        telegram.request = AsyncMock(return_value=True)
        return TelegramControl(self.journal, telegram, 123, '2')

    def callback(self, control, action, update_id=1, owner=123, chat=123, nonce=None):
        return {'update_id': update_id, 'callback_query': {'id': str(update_id), 'from': {'id': owner},
            'data': (nonce or control.nonce) + ':' + action, 'message': {'chat': {'id': chat}}}}

    async def test_wrong_sender_or_chat_cannot_control_or_get_stats(self):
        control = self.control()
        with patch.object(control, 'perform', new=AsyncMock()) as perform:
            await control.handle(self.callback(control, 'new', owner=999))
            await control.handle(self.callback(control, 'stats', update_id=2, chat=999))
            perform.assert_not_awaited()
        control.telegram.send.assert_not_awaited()

    async def test_duplicate_updates_and_old_buttons_are_not_executed(self):
        control = self.control()
        update = self.callback(control, 'status')
        with patch.object(control, 'perform', new=AsyncMock()) as perform:
            await control.handle(update)
            await control.handle(update)
            perform.assert_awaited_once_with('status')
        restarted = self.control()
        with patch.object(restarted, 'perform', new=AsyncMock()) as perform:
            await restarted.handle(update)
            await restarted.handle(self.callback(restarted, 'new', update_id=2, nonce=control.nonce))
            perform.assert_not_awaited()

    async def test_start_menu_and_status_work_without_launching_trades(self):
        control = self.control()
        await control.handle({'update_id': 1, 'message': {'chat': {'id': 123}, 'from': {'id': 123}, 'text': '/start'}})
        self.assertIn('inline_keyboard', control.telegram.send.call_args.kwargs['reply_markup'])
        await control.handle(self.callback(control, 'status', update_id=2))
        self.assertIn(self.cycle_id, control.telegram.send.call_args.args[0])
        self.assertIsNone(control.task)

    async def test_listener_start_does_not_start_trading(self):
        control = self.control()
        control.telegram.request.side_effect = asyncio.CancelledError()
        with patch('telegram_control.run_command', new=AsyncMock()) as run:
            with self.assertRaises(asyncio.CancelledError):
                await control.listen()
            run.assert_not_awaited()
        self.assertIsNone(control.task)

    async def test_new_run_uses_env_series_and_ignores_double_click(self):
        control = self.control()
        self.journal.abandon(self.cycle_id)
        entered = asyncio.Event()
        release = asyncio.Event()
        async def work(*args, **kwargs):
            entered.set()
            await release.wait()
        update = self.callback(control, 'new')
        with patch.dict(os.environ, {'AUTO_MIN_AMOUNT': '9000', 'AUTO_MAX_AMOUNT': '9500', 'AUTO_CYCLE_COUNT': '3'}), \
                patch('telegram_control.run_command', side_effect=work) as run:
            await control.handle(update)
            await entered.wait()
            update['update_id'] = 2  # A second physical press on the old start button.
            await control.handle(update)
            run.assert_awaited_once()
            args = run.call_args.args[0]
            self.assertEqual(args.p2_profile, '2')
            self.assertTrue(args.auto)
            self.assertIsNone(args.count)  # run_command reads AUTO_CYCLE_COUNT, not forced to one.
            self.assertFalse(run.call_args.kwargs['use_lock'])
            release.set()
            await control.task

    async def test_new_run_cannot_replace_unfinished_cycle(self):
        control = self.control()
        await control.handle(self.callback(control, 'new'))
        self.assertIsNone(control.task)
        self.assertIn('незавершённый', control.telegram.send.call_args.args[0])

    async def test_missing_configuration_is_reported_once(self):
        control = self.control()
        with patch('telegram_control.run_command', new=AsyncMock(side_effect=ValueError('Missing nickname'))):
            await control.work(control.new_args())
        control.telegram.send.assert_awaited_once()
        self.assertIn('Missing nickname', control.telegram.send.call_args.args[0])

    async def test_recorded_failure_does_not_generate_second_error(self):
        control = self.control()
        async def fail(*args, **kwargs):
            self.journal.transition(self.cycle_id, 'forward_paid', 'p2', 'error', 'Already queued')
            raise ValueError('Already queued')
        with patch('telegram_control.run_command', side_effect=fail):
            await control.work(control.new_args())
        control.telegram.send.assert_not_awaited()

    async def test_resume_uses_saved_cycle_not_default_profile(self):
        self.runner()  # Mark fixture cycle as automatic.
        control = self.control()
        with patch('telegram_control.run_command', new=AsyncMock()) as run:
            await control.handle(self.callback(control, 'resume'))
            await control.task
            args = run.call_args.args[0]
            self.assertEqual(args.resume, self.cycle_id)
            self.assertIsNone(args.p2_profile)

    async def test_stop_signals_worker_without_cancelling_request(self):
        control = self.control()
        gate = asyncio.Event()
        control.task = asyncio.create_task(gate.wait())
        await control.handle(self.callback(control, 'stop'))
        self.assertTrue(control.stop_event.is_set())
        self.assertFalse(control.task.cancelled())
        self.assertFalse(control.task.done())
        gate.set()
        await control.task

    async def test_stop_interrupts_delay_before_first_trade(self):
        runner = self.runner()
        runner.stop_event = asyncio.Event()
        entered = asyncio.Event()
        wait = runner.wait_delay
        async def waiting(seconds):
            entered.set()
            await wait(seconds)
        runner.wait_delay = waiting
        task = asyncio.create_task(runner.run(self.cycle_id))
        await asyncio.wait_for(entered.wait(), 1)
        runner.stop_event.set()
        with self.assertRaises(OperatorStopped):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.exchange.calls, [])
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'paused')

    async def test_stop_during_payment_saves_result_before_pause_and_resumes_once(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.stop_event = asyncio.Event()
        mark_paid = runner.clients['p2'].mark_paid
        async def paid(*args):
            await mark_paid(*args)
            runner.stop_event.set()
        runner.clients['p2'].mark_paid = paid
        with self.assertRaises(OperatorStopped):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_paid')['status'], 'done')
        self.assertFalse(any(op == 'release' for _, op, _ in self.exchange.calls))
        self.assertEqual(self.telegram.messages, [])
        runner.stop_event.clear()
        await runner.run(self.cycle_id)
        self.assertEqual(sum(op == 'paid' and order == 'ORDER-1' for _, op, order in self.exchange.calls), 1)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')

    async def test_old_queued_success_is_suppressed_but_error_is_delivered(self):
        self.journal.transition(self.cycle_id, 'cycle', 'both', 'completed', 'success')
        self.journal.transition(self.cycle_id, 'reverse_paid', 'p1', 'error', 'test failure')
        await self.reporter.flush()
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn('test failure', self.telegram.messages[0])

    def test_today_uses_krasnoyarsk_midnight_and_deduplicates_recovery(self):
        def completion(cid, time, quantity, fiat='RUB'):
            with patch('journal.now', return_value=time):
                self.journal.transition(cid, 'forward_complete', 'both', 'done', 'done',
                    context={'amount': '1000', 'quantity': quantity, 'fiat': fiat})
                self.journal.transition(cid, 'cycle', 'both', 'completed', 'done', cycle_status='completed')
        completion(self.cycle_id, '2026-09-23T16:59:59+00:00', '10')  # Yesterday locally.
        second = self.journal.create(self.spec)
        completion(second, '2026-09-23T17:00:00+00:00', '20')
        completion(second, '2026-09-24T01:00:00+00:00', '20')  # Same completion reconciled twice.
        third = self.journal.create(self.spec)
        completion(third, '2026-09-24T16:59:59+00:00', '30', 'KZT')
        fourth = self.journal.create(self.spec)
        completion(fourth, '2026-09-24T17:00:00+00:00', '40')  # Tomorrow locally.
        text = daily_stats(self.journal, datetime(2026, 9, 24, 5, tzinfo=timezone.utc))
        self.assertIn('24.09.2026', text)
        self.assertIn('Полностью завершено циклов: 2', text)
        self.assertIn('50 USDT', text)
        self.assertIn('1000 RUB', text)
        self.assertIn('1000 KZT', text)
        self.assertIn('Обратные сделки П2 → П1: 0', text)


class TelegramTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_includes_buttons_and_preserves_text_limit(self):
        bot = TelegramNotifier('fake-secret', '123')
        bot.request = AsyncMock()
        buttons = {'inline_keyboard': [[{'text': 'Status', 'callback_data': 'test:status'}]]}
        self.assertTrue(await bot.send('x' * 5000, reply_markup=buttons))
        method, payload = bot.request.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(payload['reply_markup'], buttons)
        self.assertEqual(len(payload['text']), 4000)

    async def test_http_error_does_not_expose_bot_token(self):
        bot = TelegramNotifier('SENSITIVE_TEST_TOKEN', '123')
        with patch('notifier.httpx.AsyncClient') as factory:
            client = factory.return_value.__aenter__.return_value
            client.post.side_effect = httpx.ConnectError('https://api.telegram.org/botSENSITIVE_TEST_TOKEN/getUpdates')
            with self.assertRaises(RuntimeError) as caught:
                await bot.request('getUpdates', {})
        self.assertNotIn('SENSITIVE_TEST_TOKEN', str(caught.exception))
