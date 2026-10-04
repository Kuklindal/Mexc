import asyncio
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from cycle import OperatorStopped
from telegram_control import TelegramControl, daily_stats
from notifier import TelegramNotifier
from sheets import Reporter
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

    def buttons(self, control):
        return [button['text'] for row in control.keyboard()['inline_keyboard'] for button in row]

    def test_status_explains_connection_retry_when_profiles_have_no_timer(self):
        from rollover import save_state

        self.journal.abandon(self.cycle_id)
        control = self.control()
        control.task = SimpleNamespace(done=lambda: False)
        state = {'mode': 'cash_volume', 'status': 'waiting', 'profiles': ['default'],
                 'p1_profile': 'p1', 'completed_count': 0, 'cooldowns': {},
                 'last_error': 'AdsPower Local API недоступен (ConnectError)'}
        save_state(self.journal, state)
        message = control.status()
        self.assertIn('Повторяет подключение', message)
        self.assertIn('AdsPower Local API недоступен', message)
        self.assertNotIn('Ожидание следующего доступного П2', message)

        state.pop('last_error')
        save_state(self.journal, state)
        self.assertIn('Ожидание следующего доступного П2', control.status())

        state['status'] = 'running'
        save_state(self.journal, state)
        self.assertIn('Подготовка следующего цикла', control.status())

    def uncertain_chat(self, text='Здравствуйте'):
        self.journal.transition(self.cycle_id, 'forward_ad', 'p1', 'done', 'ad',
            result={'adv_no': 'AD-SELL'})
        self.journal.transition(self.cycle_id, 'forward_create', 'p2', 'done', 'created',
            result={'order_no': 'ORDER-1'})
        self.journal.transition(self.cycle_id, 'forward_message', 'p2', 'unknown', 'connection lost',
            result={'order_no': 'ORDER-1', 'text': text}, cycle_status='paused')

    async def test_service_restart_resumes_running_series_but_not_telegram_pause(self):
        from rollover import begin, load_state, save_state
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default', 'AUTO_RESUME_ON_BOOT': 'true'}):
            state = begin(self.journal, 'all')
            state['status'] = 'running'
            save_state(self.journal, state)
            control = self.control()
            control.task = asyncio.create_task(asyncio.sleep(60))
            control.request_shutdown()
            self.assertTrue(load_state(self.journal)['resume_on_boot'])
            control.task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await control.task

            restarted = self.control()
            with patch.object(restarted, 'work_rollover', new=AsyncMock()) as worker:
                self.assertTrue(restarted.resume_after_restart())
                await restarted.task
                worker.assert_awaited_once()
            self.assertNotIn('resume_on_boot', load_state(self.journal))

            state = load_state(self.journal)
            state['status'] = 'paused'
            save_state(self.journal, state)
            self.assertFalse(self.control().resume_after_restart())

    async def test_chat_recovery_requires_explicit_confirmation_and_never_duplicates(self):
        self.uncertain_chat()
        control = self.control()
        self.assertIn('🔎 Сверить сообщение', self.buttons(control))
        self.assertNotIn('▶️ Продолжить', self.buttons(control))
        await control.perform('chat_review')
        self.assertIn('ORDER-1', control.telegram.send.call_args.args[0])
        await control.perform('chat_already')
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_message')['status'], 'unknown')
        await control.perform('chat_confirm')
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_message')['status'], 'done')
        self.assertIn('▶️ Продолжить', self.buttons(control))
        await control.perform('chat_confirm')
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_message')['status'], 'done')

    async def test_chat_retry_sends_once_with_saved_profile_and_order(self):
        from cycle import fingerprint
        self.uncertain_chat('Сохранённая фраза')
        self.spec['profiles'] = {'p2': fingerprint('P2-KEY')}
        self.spec['members'] = {'p1': 'MEMBER-P1', 'p2': 'MEMBER-P2'}
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        control = self.control()
        client = SimpleNamespace(get_order_detail=AsyncMock(return_value={
            'advOrderNo': 'ORDER-1', 'advNo': 'AD-SELL', 'coinName': 'USDT', 'fiatUnit': 'RUB',
            'userInfo': {'memberId': 'MEMBER-P1', 'nickName': 'Trusted-P1'}}),
            send_chat_text=AsyncMock(), close=AsyncMock())
        settings = SimpleNamespace(api_key='P2-KEY', secret_key='secret', base_url='https://example.test',
                                   recv_window=5000, proxy_url=None)
        await control.perform('chat_review')
        await control.perform('chat_retry')
        client.send_chat_text.assert_not_awaited()
        with patch('config.Settings.from_env', return_value=settings) as from_env, \
                patch('mexc_client.MexcP2PClient', return_value=client):
            await control.perform('chat_confirm')
        from_env.assert_called_once_with('p2', p2_profile='default')
        client.send_chat_text.assert_awaited_once_with('ORDER-1', 'Сохранённая фраза')
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_message')['status'], 'done')
        await control.perform('chat_confirm')
        client.send_chat_text.assert_awaited_once()

    async def test_chat_retry_error_stays_unknown_and_requires_new_review(self):
        from cycle import fingerprint
        self.uncertain_chat()
        self.spec['profiles'] = {'p2': fingerprint('P2-KEY')}
        self.spec['members'] = {'p1': 'MEMBER-P1', 'p2': 'MEMBER-P2'}
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        control = self.control()
        client = SimpleNamespace(get_order_detail=AsyncMock(return_value={
            'advOrderNo': 'ORDER-1', 'advNo': 'AD-SELL', 'coinName': 'USDT', 'fiatUnit': 'RUB',
            'userInfo': {'memberId': 'MEMBER-P1', 'nickName': 'Trusted-P1'}}),
            send_chat_text=AsyncMock(side_effect=TimeoutError()), close=AsyncMock())
        settings = SimpleNamespace(api_key='P2-KEY', secret_key='secret', base_url='https://example.test',
                                   recv_window=5000, proxy_url=None)
        await control.perform('chat_review')
        await control.perform('chat_retry')
        with patch('config.Settings.from_env', return_value=settings), \
                patch('mexc_client.MexcP2PClient', return_value=client):
            await control.perform('chat_confirm')
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_message')['status'], 'unknown')
        self.assertIsNone(control.chat_recovery)
        self.assertIn('автоматического повтора нет', control.telegram.send.call_args.args[0])

    async def test_chat_retry_rejects_wrong_counterparty_before_sending(self):
        from cycle import fingerprint
        self.uncertain_chat()
        self.spec['profiles'] = {'p2': fingerprint('P2-KEY')}
        self.spec['members'] = {'p1': 'MEMBER-P1', 'p2': 'MEMBER-P2'}
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        control = self.control()
        client = SimpleNamespace(get_order_detail=AsyncMock(return_value={
            'advOrderNo': 'ORDER-1', 'advNo': 'AD-SELL', 'coinName': 'USDT', 'fiatUnit': 'RUB',
            'userInfo': {'memberId': 'OUTSIDER', 'nickName': 'Trusted-P1'}}),
            send_chat_text=AsyncMock(), close=AsyncMock())
        settings = SimpleNamespace(api_key='P2-KEY', secret_key='secret', base_url='https://example.test',
                                   recv_window=5000, proxy_url=None)
        await control.perform('chat_review')
        await control.perform('chat_retry')
        with patch('config.Settings.from_env', return_value=settings), \
                patch('mexc_client.MexcP2PClient', return_value=client):
            await control.perform('chat_confirm')
        client.send_chat_text.assert_not_awaited()
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_message')['status'], 'unknown')

    async def test_menu_only_shows_actions_available_for_current_state(self):
        control = self.control()
        self.assertIn('▶️ Продолжить', self.buttons(control))
        self.assertIn('🗑 Сбросить цикл', self.buttons(control))
        self.assertNotIn('🔄 Все профили', self.buttons(control))
        self.assertNotIn('⏹ Остановить', self.buttons(control))
        self.journal.abandon(self.cycle_id)
        self.assertIn('💵 Объём наличка', self.buttons(control))
        self.assertIn('📈 Объём Eflp', self.buttons(control))
        self.assertIn('👥 Уникальные Eflp', self.buttons(control))
        self.assertNotIn('🗑 Сбросить цикл', self.buttons(control))
        self.assertNotIn('▶️ Продолжить', self.buttons(control))
        self.assertNotIn('default', self.buttons(control))
        await control.perform('profiles')
        from config import p2_nickname
        self.assertIn(p2_nickname('default'), self.buttons(control))
        self.assertIn('↩️ Назад', self.buttons(control))
        await control.perform('back')
        self.assertNotIn('default', self.buttons(control))

    async def test_cash_button_launches_configured_profiles_without_wizard(self):
        from test_trade_modes import env_for
        from rollover import load_state
        self.journal.abandon(self.cycle_id)
        env = env_for(2)
        env.update({'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788',
                    'CASH_VOLUME_P1_PROFILE': 'p1',
                    'CASH_VOLUME_P2_PROFILES': '2,1'})
        with patch.dict(os.environ, env):
            control = self.control()
            with patch.object(control, 'work_rollover', new_callable=AsyncMock):
                await control.perform('cash_volume')
                await asyncio.sleep(0)
            self.assertIsNone(control.menu)
            self.assertEqual(load_state(self.journal)['profiles'], ['2', '1'])
            self.assertEqual(load_state(self.journal)['p1_profile'], 'p1')

    async def test_eflp_volume_chooses_only_p1_then_uses_configured_p2(self):
        from test_trade_modes import env_for
        from rollover import load_state
        self.journal.abandon(self.cycle_id)
        env = env_for(2) | {'MEXC_P1_BUY_ADV_NO': 'a1234567890123456788',
                            'MEXC_P2_2_BUY_ADV_NO': 'a1234567890123456787',
                            'MEXC_P2_1_PAYMENT_ID': '1001',
                            'MEXC_P2_2_PAYMENT_ID': '1002',
                            'EFLP_VOLUME_P2_PROFILES': '2,1'}
        with patch.dict(os.environ, env):
            control = self.control()
            await control.perform('eflp_volume')
            self.assertEqual(control.menu, 'cash_p1')
            self.assertEqual(control.selected_mode, 'eflp_volume')
            self.assertFalse(any(button['callback_data'].endswith('cash_p1:2')
                                 for row in control.keyboard()['inline_keyboard'] for button in row))
            with patch.object(control, 'work_rollover', new_callable=AsyncMock):
                await control.perform('cash_p1:p1')
                await asyncio.sleep(0)
            self.assertIsNone(control.menu)
            self.assertEqual(load_state(self.journal)['profiles'], ['2', '1'])
            self.assertEqual(load_state(self.journal)['p1_profile'], 'p1')

    async def test_eflp_unique_keeps_manual_p1_and_p2_selection(self):
        self.journal.abandon(self.cycle_id)
        control = self.control()
        await control.perform('eflp_unique')
        self.assertIsNone(control.task)
        self.assertEqual(control.menu, 'cash_p1')
        self.assertEqual(control.selected_mode, 'eflp_unique')

    async def test_unique_toggle_edits_same_telegram_menu(self):
        from test_trade_modes import env_for
        self.journal.abandon(self.cycle_id)
        with patch.dict(os.environ, env_for(2)):
            control = self.control()
            control.menu = 'unique_p2'
            control.unique_p1 = 'p1'
            await control.perform('unique_p2:1', callback_message_id=17)
        self.assertEqual(control.unique_p2, {'1'})
        self.assertEqual(control.telegram.request.await_args.args[0], 'editMessageText')
        control.telegram.send.assert_not_awaited()

    async def test_menu_shows_reset_during_return_and_only_stop_while_running(self):
        from rollover import save_state
        save_state(self.journal, {'status': 'paused', 'active_cycle': self.cycle_id,
            'pending_return': {'cycle_id': self.cycle_id}, 'cooldowns': {}})
        control = self.control()
        self.assertIn('▶️ Продолжить', self.buttons(control))
        self.assertIn('🗑 Сбросить цикл', self.buttons(control))
        self.assertNotIn('🔀 Сменить П2', self.buttons(control))
        gate = asyncio.Event()
        control.task = asyncio.create_task(gate.wait())
        try:
            self.assertIn('⏹ Остановить', self.buttons(control))
            self.assertNotIn('▶️ Продолжить', self.buttons(control))
            self.assertNotIn('🗑 Сбросить цикл', self.buttons(control))
        finally:
            gate.set()
            await control.task

    async def test_error_notification_contains_resume_button_while_worker_finishes(self):
        control = self.control()
        gate = asyncio.Event()
        control.task = asyncio.create_task(gate.wait())
        try:
            self.journal.transition(self.cycle_id, 'reverse_replenish', 'p1', 'error',
                'AdsPower timeout', cycle_status='paused')
            reporter = Reporter(self.journal, control.telegram, None,
                                keyboard=lambda: control.keyboard(force_idle=True))
            await reporter.flush()
            sent_keyboard = control.telegram.send.call_args.kwargs['reply_markup']
            actions = [button['callback_data'].split(':', 1)[1]
                       for row in sent_keyboard['inline_keyboard'] for button in row]
            self.assertIn('resume', actions)
        finally:
            gate.set()
            await control.task

    async def test_menu_shows_profile_switch_only_for_idle_active_series(self):
        from rollover import save_state
        self.journal.abandon(self.cycle_id)
        save_state(self.journal, {'status': 'paused', 'active_cycle': None,
            'pending_return': None, 'cooldowns': {}, 'profiles': ['default', '2']})
        control = self.control()
        self.assertIn('🔀 Сменить П2', self.buttons(control))
        self.assertIn('🧹 Завершить серию', self.buttons(control))
        self.assertNotIn('⇄ 2', self.buttons(control))
        await control.perform('switch')
        from config import p2_nickname
        self.assertIn('⇄ ' + p2_nickname('2'), self.buttons(control))

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
        self.assertIn('👤 Сейчас П2:', control.telegram.send.call_args.args[0])
        self.assertNotIn('П2 для нового запуска:', control.telegram.send.call_args.args[0])
        self.assertNotIn('Цикл: ' + self.cycle_id, control.telegram.send.call_args.args[0])
        self.assertIsNone(control.task)

    async def test_status_shows_active_profile_without_archive_or_default_profile(self):
        from rollover import save_state

        spec = dict(self.spec, p2_profile='4', nicknames={'p2': 'Dimazz82'})
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?',
                                (json.dumps(spec), self.cycle_id))
        self.journal.db.commit()
        self.journal.transition(self.cycle_id, 'forward_release', 'p1', 'waiting',
                                'Проверка получения денег')
        save_state(self.journal, {'status': 'running', 'mode': 'all', 'profiles': ['default', '4'],
            'completed_count': 16, 'active_cycle': self.cycle_id, 'pending_return': None,
            'cooldowns': {}, 'archived_returns': [{'cycle_id': 'old-cycle', 'profile': '4',
                                                   'stage': 'withdraw_intent'}]})
        control = self.control()
        text = control.status()
        self.assertIn('Сейчас П2: Dimazz82', text)
        self.assertIn('Завершено циклов: 16', text)
        self.assertIn('П1 проверяет получение денег', text)
        self.assertNotIn('Kuk_family818', text)
        self.assertNotIn(self.cycle_id, text)
        self.assertNotIn('old-cycle', text)
        self.assertNotIn('withdraw_intent', text)

    async def test_profile_buttons_and_status_show_nicknames_but_keep_profile_callbacks(self):
        self.journal.abandon(self.cycle_id)
        control = self.control()
        with patch.dict(os.environ, {'ROLLOVER_PROFILES': 'default,2',
                                      'MEXC_P2_NICKNAME': 'Kuk_family818',
                                      'MEXC_P2_2_NICKNAME': 'kukish'}):
            control.menu = 'start_profiles'
            buttons = [button for row in control.keyboard()['inline_keyboard'] for button in row]
            self.assertTrue(any(b['text'] == 'kukish' and b['callback_data'].endswith(':single:2')
                                for b in buttons))
            self.assertNotIn('П2 для нового запуска:', control.status())

    async def test_listener_start_does_not_start_trading(self):
        control = self.control()
        control.telegram.request.side_effect = asyncio.CancelledError()
        with patch('telegram_control.run_command', new=AsyncMock()) as run:
            with self.assertRaises(asyncio.CancelledError):
                await control.listen()
            run.assert_not_awaited()
        self.assertIsNone(control.task)

    async def test_listener_closes_other_adspower_profiles(self):
        control = self.control()
        control.browser_guard = SimpleNamespace(
            api_key='test', profile_id='P1',
            close_other_local_profiles=AsyncMock(return_value=2))
        control.telegram.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await control.listen()
        control.browser_guard.close_other_local_profiles.assert_awaited_once()

    async def test_listener_retries_pending_google_sale(self):
        self.journal.transition(self.cycle_id, 'forward_complete', 'both', 'done', 'done',
            context={'amount': '9000', 'quantity': '100', 'fiat': 'RUB'})
        control = self.control()
        sheets = type('Sheets', (), {})()
        sheets.prepare = AsyncMock()
        sheets.send = AsyncMock()
        sheets.send_weekly = AsyncMock()
        control.sheets = sheets
        control.telegram.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await control.listen()
        sheets.send.assert_awaited_once()
        self.assertEqual(self.journal.pending_sales(), [])

    async def test_listener_shutdown_exits_without_starting_trades(self):
        control = self.control()
        control.shutdown_event.set()
        await control.listen()
        control.telegram.request.assert_not_awaited()
        self.assertIsNone(control.task)

    async def test_new_run_ignores_double_click(self):
        control = self.control()
        self.journal.abandon(self.cycle_id)
        entered = asyncio.Event()
        release = asyncio.Event()
        async def work(*args, **kwargs):
            entered.set()
            await release.wait()
        update = self.callback(control, 'new')
        with patch.dict(os.environ, {'AUTO_MIN_AMOUNT': '9000', 'AUTO_MAX_AMOUNT': '9500'}), \
                patch('telegram_control.run_command', side_effect=work) as run:
            await control.handle(update)
            await entered.wait()
            update['update_id'] = 2  # A second physical press on the old start button.
            await control.handle(update)
            run.assert_awaited_once()
            args = run.call_args.args[0]
            self.assertEqual(args.p2_profile, '2')
            self.assertTrue(args.auto)
            self.assertIsNone(args.count)  # Standalone cycle --auto defaults to one.
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

    async def test_rollover_http_error_is_reported_without_crashing_error_handler(self):
        from mexc_client import MexcAPIError
        control = self.control()
        error = MexcAPIError('MEXC HTTP 504', code=504, http_status=504)
        with patch('rollover.run', new=AsyncMock(side_effect=error)):
            await control.work_rollover({})
        control.telegram.send.assert_awaited_once()
        self.assertIn('MEXC HTTP 504', control.telegram.send.call_args.args[0])

    async def test_return_failure_is_reported_after_limit_event(self):
        from mexc_client import MexcAPIError

        control = self.control()
        state = {'pending_return': {'cycle_id': self.cycle_id,
                                    'profile': 'default', 'stage': 'withdraw_intent'}}
        async def fail(*args, **kwargs):
            self.journal.transition(self.cycle_id, 'reverse_create', 'p2', 'paused', 'MEXC 60085')
            raise MexcAPIError('Withdrawal permission denied', code=700007, http_status=400)

        with patch('rollover.run', side_effect=fail):
            await control.work_rollover(state)
        control.telegram.send.assert_awaited_once()
        alert = control.telegram.send.call_args.args[0]
        self.assertIn('withdraw_intent', alert)
        self.assertIn('Withdrawal permission denied', alert)
        self.assertIn(self.cycle_id, alert)

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

    async def test_selecting_same_profile_resumes_legacy_cycle_and_other_cannot_replace_it(self):
        self.runner()
        control = self.control()
        await control.handle(self.callback(control, 'single:2'))
        self.assertIsNone(control.task)
        self.assertIn('Нажми «Продолжить»', control.telegram.send.call_args.args[0])
        with patch('telegram_control.run_command', new=AsyncMock()) as run:
            await control.handle(self.callback(control, 'single:default', update_id=2))
            await control.task
        self.assertEqual(run.call_args.args[0].resume, self.cycle_id)
        self.assertIsNone(run.call_args.args[0].p2_profile)

    async def test_stop_reports_already_paused_cycle(self):
        control = self.control()
        await control.handle(self.callback(control, 'stop'))
        self.assertIn(self.cycle_id, control.telegram.send.call_args.args[0])
        self.assertIsNone(control.task)

    async def test_reset_cycle_in_bot_requires_second_click_and_does_not_touch_mexc(self):
        control = self.control()
        await control.handle(self.callback(control, 'reset'))
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'active')
        self.assertIn('Сбросить цикл', control.telegram.send.call_args.args[0])
        self.assertEqual(control.reset_target, self.cycle_id)
        await control.handle(self.callback(control, 'reset_confirm:' + self.cycle_id, update_id=2))
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'abandoned')
        self.assertIsNone(control.reset_target)
        self.assertIsNone(control.task)
        self.assertIsInstance(self.journal.create(self.spec), str)

    async def test_reset_rejects_stale_confirmation_and_active_worker(self):
        control = self.control()
        await control.handle(self.callback(control, 'reset_confirm:' + self.cycle_id))
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'active')
        gate = asyncio.Event()
        control.task = asyncio.create_task(gate.wait())
        await control.handle(self.callback(control, 'reset', update_id=2))
        self.assertIn('Сначала нажми «Остановить»', control.telegram.send.call_args.args[0])
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'active')
        gate.set()
        await control.task

    async def test_reset_stops_saved_scheduler_but_preserves_profile_timers(self):
        from rollover import load_state, save_state
        state = {'status': 'paused', 'active_cycle': self.cycle_id, 'pending_return': None,
                 'known_cycle_ids': [], 'cooldowns': {'default': {'until': '2026-09-30T00:00:00+00:00'}}}
        save_state(self.journal, state)
        control = self.control()
        await control.perform('reset')
        await control.perform('reset_confirm:' + self.cycle_id)
        saved = load_state(self.journal)
        self.assertEqual(saved['status'], 'stopped')
        self.assertIsNone(saved['active_cycle'])
        self.assertEqual(saved['last_cycle_rowid'],
                         self.journal.db.execute('SELECT rowid FROM cycles WHERE id=?',
                                                 (self.cycle_id,)).fetchone()[0])
        self.assertEqual(saved['cooldowns'], state['cooldowns'])

    async def test_reset_archives_unfinished_return_after_confirmation(self):
        from rollover import load_state, save_state
        save_state(self.journal, {'pending_return': {'cycle_id': self.cycle_id,
            'profile': 'default', 'stage': 'withdraw_intent', 'quantity': '1939.3881',
            'network_label': 'BEP20', 'request_id': 'withdraw-123'},
            'active_cycle': self.cycle_id, 'status': 'paused', 'mode': 'single',
            'profiles': ['default'], 'cooldowns': {'default': {'until': '2099-09-30T00:00:00+00:00'}}})
        control = self.control()
        await control.perform('reset')
        self.assertIn('Сбросить цикл', control.telegram.send.call_args.args[0])
        self.assertIn('⚠️ Подтвердить сброс', self.buttons(control))
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'active')
        await control.perform('reset_confirm:' + self.cycle_id)
        saved = load_state(self.journal)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'abandoned')
        self.assertEqual(saved['status'], 'stopped')
        self.assertIsNone(saved['pending_return'])
        self.assertIsNone(saved['active_cycle'])
        self.assertEqual(saved['archived_returns'][0]['stage'], 'withdraw_intent')
        self.assertEqual(saved['cooldowns']['default']['until'], '2099-09-30T00:00:00+00:00')
        self.assertEqual(saved['archived_returns'][0]['request_id'], 'withdraw-123')
        self.assertIsInstance(self.journal.create(self.spec), str)

    async def test_timer_menu_resets_only_chosen_p2_after_confirmation(self):
        from rollover import load_state, save_state

        self.journal.abandon(self.cycle_id)
        future = '2099-09-30T00:00:00+00:00'
        save_state(self.journal, {'status': 'paused', 'mode': 'all', 'profiles': ['default', '2'],
            'active_cycle': None, 'pending_return': None,
            'cooldowns': {'default': {'until': future, 'manual_block': False},
                          '2': {'until': future, 'manual_block': False}}})
        control = self.control()
        self.assertIn('⏱ Таймеры П2', self.buttons(control))
        await control.perform('timers')
        await control.perform('timer:2')
        self.assertIn('⚠️ Сбросить таймер', self.buttons(control))
        self.assertIn('MEXC', control.telegram.send.call_args.args[0])
        self.assertIn('2', load_state(self.journal)['cooldowns'])
        await control.perform('timer_confirm:2')
        saved = load_state(self.journal)
        self.assertNotIn('2', saved['cooldowns'])
        self.assertIn('default', saved['cooldowns'])

    async def test_timer_reset_requires_fresh_confirmation(self):
        from rollover import load_state, save_state

        save_state(self.journal, {'status': 'paused', 'mode': 'single', 'profiles': ['default'],
            'active_cycle': self.cycle_id, 'pending_return': None,
            'cooldowns': {'default': {'until': '2099-09-30T00:00:00+00:00'}}})
        control = self.control()
        await control.perform('timer_confirm:default')
        self.assertIn('default', load_state(self.journal)['cooldowns'])
        await control.perform('timers')
        await control.perform('timer:default')
        await control.perform('timer_cancel')
        self.assertIn('default', load_state(self.journal)['cooldowns'])

    async def test_add_profile_deletes_private_message_before_saving(self):
        control = self.control()
        update = {'update_id': 1, 'message': {'chat': {'id': 123}, 'from': {'id': 123},
            'message_id': 456, 'text': '/addprofile new APIKEY SECRET MEMBER Nick 2642995'}}
        with patch('profile_registry.add_profile', return_value='new') as add:
            await control.handle(update)
        control.telegram.request.assert_awaited_once_with('deleteMessage', {'chat_id': '123', 'message_id': 456})
        add.assert_called_once()
        self.assertIn('П2 new добавлен', control.telegram.send.call_args.args[0])

    async def test_add_profile_with_proxy_keeps_credentials_out_of_reply(self):
        control = self.control()
        proxy = 'http://login:secret@proxy.example:8080'
        update = {'update_id': 1, 'message': {'chat': {'id': 123}, 'from': {'id': 123},
            'message_id': 456,
            'text': '/addprofile new APIKEY SECRET MEMBER Nick 2642995 ' + proxy}}
        with patch('profile_registry.add_profile', return_value='new') as add:
            await control.handle(update)
        self.assertEqual(add.call_args.args[1][-1], proxy)
        reply = control.telegram.send.call_args.args[0]
        self.assertIn('Прокси сохранён', reply)
        self.assertNotIn(proxy, reply)
        self.assertNotIn('APIKEY', reply)

    async def test_add_profile_fails_closed_when_message_cannot_be_deleted(self):
        control = self.control()
        control.telegram.request.return_value = False
        update = {'update_id': 1, 'message': {'chat': {'id': 123}, 'from': {'id': 123},
            'message_id': 456, 'text': '/addprofile new APIKEY SECRET MEMBER Nick 2642995'}}
        with patch('profile_registry.add_profile') as add:
            await control.handle(update)
        add.assert_not_called()
        self.assertIn('профиль не сохранён', control.telegram.send.call_args.args[0])

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

    async def test_telegram_failure_identifies_operation_without_exposing_token(self):
        bot = TelegramNotifier('SENSITIVE_TEST_TOKEN', '123')
        request = httpx.Request('POST', 'https://api.telegram.org/botSENSITIVE_TEST_TOKEN/getUpdates')
        response = httpx.Response(409, request=request,
                                  json={'ok': False, 'error_code': 409,
                                        'description': 'SENSITIVE_TEST_TOKEN'})
        with patch('notifier.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            with self.assertRaises(RuntimeError) as caught:
                await bot.request('getUpdates', {})
        self.assertIn('getUpdates: HTTP 409', str(caught.exception))
        self.assertIn('another getUpdates poller', str(caught.exception))
        self.assertNotIn('SENSITIVE_TEST_TOKEN', str(caught.exception))
