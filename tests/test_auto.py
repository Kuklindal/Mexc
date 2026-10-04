import json
import copy
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

from cycle import (AdsPowerPreflightUnavailable, AutoConsole, CycleRunner, Paused,
                   STEPS, MUTATING_ACTIONS, action_delay, delay_bounds)
from mexc_client import MexcAPIError, MexcReadUnavailable
from adspower import AdsPowerTimeout, AdsPowerUnavailable
import test_cycle as fixtures


class AutoTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CycleTests.setUp
    tearDown = fixtures.CycleTests.tearDown

    def runner(self):
        self.spec.update(automatic=True, forward_adv_no="AD-SELL", reverse_adv_no="AD-BUY")
        self.journal.db.execute("UPDATE cycles SET spec=? WHERE id=?", (json.dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        browser = type("Browser", (), {})()
        browser.open_order = AsyncMock(return_value="ready")
        browser.inspect = AsyncMock(return_value="passed")
        browser.approve = AsyncMock()
        browser.close_order_tabs = AsyncMock(return_value=1)
        runner = CycleRunner(self.journal, self.reporter,
            {a: self.exchange.client(a) for a in ("p1", "p2")}, AutoConsole(write=lambda _: None),
            state_changes=True, p2_payment_id="2642995", pay_method_id="578", browser=browser,
            automatic=True, delay_seconds=20, trusted_members={"p1": "MEMBER-P1", "p2": "MEMBER-P2"},
            trusted_nicknames={"p2": "Trusted-P2"})
        return runner

    async def test_p2_maker_return_uses_p2_ad_and_p1_as_buyer(self):
        runner = self.runner()
        self.spec.update(reverse_maker='p2', reverse_adv_no='AD-P2-SELL', buy_replenish=False)
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        p2_ad = copy.deepcopy(self.exchange.ad)
        p2_ad.update(advNo='AD-P2-SELL', availableQuantity='0', advStatus='OPEN',
                     overVerify=None)

        async def p2_get_ad(adv_no):
            self.assertEqual(adv_no, 'AD-P2-SELL')
            return copy.deepcopy(p2_ad)

        async def p2_ad_details(adv_no):
            self.assertEqual(adv_no, 'AD-P2-SELL')
            return {'id': adv_no, 'coinName': 'USDT', 'currency': 'RUB',
                    'tradeType': 1, 'availableQuantity': p2_ad['availableQuantity'],
                    'overVerify': {'types': [1]}}

        async def p2_replenish(plan):
            p2_ad['availableQuantity'] = str(Decimal(p2_ad['availableQuantity'])
                                                 + Decimal(plan['quantity']))

        maker_browser = type('Browser', (), {})()
        maker_browser.ad_details = AsyncMock(side_effect=p2_ad_details)
        maker_browser.replenish_ad = AsyncMock(side_effect=p2_replenish)
        maker_browser.open_order = AsyncMock(return_value='ready')
        maker_browser.inspect = AsyncMock(return_value='passed')
        maker_browser.approve = AsyncMock()
        maker_browser.close_order_tabs = AsyncMock(return_value=1)
        maker_browser.profile_id = 'p2-browser'
        maker_browser.stop_profile = AsyncMock()
        runner.browser.profile_id = 'p1-browser'
        runner.maker_browser = maker_browser
        runner.clients['p2'].get_ad = AsyncMock(side_effect=p2_get_ad)
        runner.delay_seconds = runner.delay_max_seconds = 0
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(maker_browser.replenish_ad.await_count, 1)
        self.assertEqual(maker_browser.approve.await_count, 1)
        maker_browser.stop_profile.assert_awaited_once()
        reverse_create = next(call for call in self.exchange.calls
                              if call[0] == 'p1' and call[1] == 'create')
        self.assertEqual(reverse_create[2]['adv_no'], 'AD-P2-SELL')
        self.assertEqual(reverse_create[2]['amount'], '10000.00')
        self.assertEqual(reverse_create[2]['user_confirm_pay_method_id'], 578)
        self.assertIsNone(self.journal.step(self.cycle_id, 'reverse_replenish_buy'))

    async def test_p2_maker_records_sub_cent_rounding_without_overselling(self):
        runner = self.runner()
        runner.cycle_id = self.cycle_id
        runner.spec = dict(self.spec, reverse_maker='p2')
        self.journal.transition(self.cycle_id, 'forward_complete', 'both', 'done', 'sale',
            result={'quantity': '100'}, context={'amount': '10000', 'quantity': '100'})
        self.journal.transition(self.cycle_id, 'reverse_ad', 'p2', 'done', 'ad',
            result={'adv_no': 'AD-P2-SELL'})
        self.journal.transition(self.cycle_id, 'reverse_create', 'p1', 'done', 'order',
            result={'order_no': 'ORDER-1'})
        self.exchange.orders['ORDER-1'] = {'advOrderNo': 'ORDER-1', 'advNo': 'AD-P2-SELL',
            'coinName': 'USDT', 'fiatUnit': 'RUB', 'state': 'NOT_PAID',
            'amount': '9999.99', 'tradableQuantity': '99.9999'}
        self.assertEqual((await runner.snapshot('reverse'))['residual_usdt'], '0.0001')
        self.exchange.orders['ORDER-1']['tradableQuantity'] = '99.99'
        with self.assertRaisesRegex(ValueError, 'не покрывает первую'):
            await runner.snapshot('reverse')

    def outsider(self):
        self.exchange.orders["OUTSIDER"] = {"advOrderNo": "OUTSIDER", "advNo": "AD-SELL", "state": "NOT_PAID",
            "userInfo": {"memberId": "ANOTHER-USER", "nickName": "same nickname"}}

    async def test_buy_ad_is_refilled_before_sell_ad_once(self):
        runner = self.runner()
        self.spec['buy_replenish'] = True
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        self.exchange.ad['overVerify'] = None
        buy = copy.deepcopy(self.exchange.ad)
        buy.update(advNo='AD-BUY', side='BUY', availableQuantity='20')
        async def get_ad(adv_no):
            return copy.deepcopy(buy if adv_no == 'AD-BUY' else self.exchange.ad)
        async def ad_details(adv_no):
            ad = buy if adv_no == 'AD-BUY' else self.exchange.ad
            return {'id': adv_no, 'coinName': 'USDT', 'currency': 'RUB',
                    'tradeType': 0 if adv_no == 'AD-BUY' else 1,
                    'availableQuantity': ad['availableQuantity'],
                    'overVerify': None if adv_no == 'AD-BUY' else {'types': [1]}}
        applied = []
        async def replenish(plan):
            applied.append(plan['adv_no'])
            ad = buy if plan['adv_no'] == 'AD-BUY' else self.exchange.ad
            ad['availableQuantity'] = str(Decimal(ad['availableQuantity']) + Decimal(plan['quantity']))
        runner.clients['p1'].get_ad = AsyncMock(side_effect=get_ad)
        runner.browser.ad_details = AsyncMock(side_effect=ad_details)
        runner.browser.replenish_ad = AsyncMock(side_effect=replenish)
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await runner.run(self.cycle_id)
            await runner.run(self.cycle_id)
        self.assertEqual(applied, ['AD-BUY', 'AD-SELL'])
        self.assertEqual(buy['availableQuantity'], '120')
        self.assertEqual(self.exchange.ad['availableQuantity'], '109')
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_replenish_buy')['result']['quantity'], '100')

    async def test_full_auto_cycle_delays_phrases_receipts_and_one_notification(self):
        runner = self.runner()
        with patch("cycle.asyncio.sleep", new=AsyncMock()) as sleep, patch("builtins.input", side_effect=AssertionError("stdin used")):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")
        runner.browser.approve.assert_awaited_once_with("ORDER-1")
        runner.browser.close_order_tabs.assert_awaited_once_with(['ORDER-1', 'ORDER-2'])
        self.assertEqual(sum(c.args[0] for c in sleep.call_args_list), 20 * sum(s.key.split('_', 1)[1] in MUTATING_ACTIONS for s in STEPS))
        self.assertEqual(self.exchange.calls[0][2]["user_confirm_pay_method_id"], 578)
        self.assertEqual(self.exchange.calls[5][2]["user_confirm_payment_id"], 2642995)
        from phrases import PHRASES
        for key in PHRASES:
            result = self.journal.step(self.cycle_id, key)["result"]
            self.assertIn(result["text"], PHRASES[key])
            self.assertFalse(result["chat_read"])
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.journal.sales()[0]["quantity"], "100")
        self.assertTrue(self.journal.sales()[0]["completed_at"])

    async def test_unknown_chat_keeps_exact_phrase_for_telegram_recovery(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.clients['p2'].send_chat_text = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, 'forward_message')
        self.assertEqual(saved['status'], 'unknown')
        self.assertEqual(saved['result']['order_no'], 'ORDER-1')
        from phrases import PHRASES
        self.assertIn(saved['result']['text'], PHRASES['forward_message'])

    async def test_auto_resume_uses_verified_document_check_without_second_click(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.approve.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_check')['status'], 'unknown')
        runner.browser.approve.side_effect = None
        runner.browser.open_order.return_value = 'passed'
        await runner.run(self.cycle_id)
        runner.browser.approve.assert_awaited_once_with('ORDER-1')
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')

    async def test_ads_local_api_timeout_before_action_keeps_cycle_resumable(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.open_order.side_effect = AdsPowerUnavailable('Local API ReadTimeout')
        with self.assertRaises(AdsPowerUnavailable):
            await runner.run(self.cycle_id)
        self.assertIsNone(self.journal.step(self.cycle_id, 'forward_check'))
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'paused')
        self.assertEqual(self.telegram.messages, [])
        runner.browser.open_order.side_effect = None
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')

    async def test_forward_paid_browser_read_timeout_retries_without_premature_payment(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.inspect.side_effect = AdsPowerTimeout(
            'AdsPower: команда Runtime.evaluate не ответила за 10 секунд')
        with self.assertRaises(AdsPowerPreflightUnavailable):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_paid')['status'], 'pending')
        self.assertFalse(any(op == 'paid' for _, op, _ in self.exchange.calls))
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'paused')
        runner.browser.inspect.side_effect = None
        runner.browser.inspect.return_value = 'passed'
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(sum(op == 'paid' for _, op, _ in self.exchange.calls), 2)

    async def test_legacy_forward_paid_browser_timeout_recovers_only_first_attempt(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.inspect.side_effect = AdsPowerTimeout(
            'AdsPower: команда Runtime.evaluate не ответила за 10 секунд')
        with self.assertRaises(AdsPowerPreflightUnavailable):
            await runner.run(self.cycle_id)
        self.journal.transition(self.cycle_id, 'forward_paid', 'p2', 'unknown',
                                'Результат требует сверки на MEXC (AdsPowerTimeout)',
                                result={'payment_account_id': 123})
        self.journal.transition(self.cycle_id, 'forward_paid', 'p2', 'error',
                                'AdsPower: команда Runtime.evaluate не ответила за 10 секунд')
        runner.browser.inspect.side_effect = None
        runner.browser.inspect.return_value = 'passed'
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(sum(op == 'paid' for _, op, _ in self.exchange.calls), 2)

    async def test_forward_check_read_timeout_on_resume_waits_without_marking_paid(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.inspect.side_effect = AdsPowerTimeout('read timed out')
        with self.assertRaises(AdsPowerPreflightUnavailable):
            await runner.run(self.cycle_id)
        with self.assertRaises(AdsPowerPreflightUnavailable):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_paid')['status'], 'pending')
        self.assertFalse(any(op == 'paid' for _, op, _ in self.exchange.calls))

    async def test_uncertain_mark_paid_request_is_never_retried_automatically(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.clients['p2'].mark_paid = AsyncMock(side_effect=TimeoutError('mark-paid response lost'))
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_paid')['status'], 'unknown')
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(runner.clients['p2'].mark_paid.await_count, 1)

    async def test_order_read_timeout_before_release_resumes_without_duplicate_release(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        read = runner.clients['p1'].get_order_detail
        failures = 0

        async def read_with_one_timeout(order_no):
            nonlocal failures
            waiting = self.journal.db.execute("""SELECT 1 FROM events WHERE cycle_id=?
                AND step='forward_release' AND status='waiting' LIMIT 1""",
                (self.cycle_id,)).fetchone()
            if waiting and failures == 0:
                failures += 1
                raise MexcReadUnavailable('MEXC read request failed (ReadTimeout); no action was sent')
            return await read(order_no)

        runner.clients['p1'].get_order_detail = read_with_one_timeout
        with self.assertRaises(MexcReadUnavailable):
            await runner.run(self.cycle_id)
        self.assertEqual(failures, 1)
        self.assertIsNone(self.journal.step(self.cycle_id, 'forward_release'))
        self.assertFalse(any(op == 'release' for _, op, _ in self.exchange.calls))
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(sum(op == 'release' for _, op, _ in self.exchange.calls), 2)

    async def test_post_release_timeout_remains_unknown_until_exchange_reconciliation(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        self.exchange.fail = 'release'
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_release')['status'], 'unknown')
        self.assertEqual(sum(op == 'release' for _, op, _ in self.exchange.calls), 1)
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(sum(op == 'release' for _, op, _ in self.exchange.calls), 2)

    async def test_read_timeout_after_release_reconciles_without_second_release(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        read = runner.clients['p1'].get_order_detail
        failures = 0

        async def read_after_release(order_no):
            nonlocal failures
            if (order_no == 'ORDER-1' and self.exchange.orders[order_no]['state'] == 'DONE'
                    and failures == 0):
                failures += 1
                raise MexcReadUnavailable('MEXC read request failed (ReadTimeout); no action was sent')
            return await read(order_no)

        runner.clients['p1'].get_order_detail = read_after_release
        with self.assertRaises(MexcReadUnavailable):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_release')['status'], 'unknown')
        self.assertEqual(sum(op == 'release' for _, op, _ in self.exchange.calls), 1)
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(sum(op == 'release' for _, op, _ in self.exchange.calls), 2)

    async def test_ads_local_api_timeout_before_quantity_request_is_retryable(self):
        del self.exchange.ad['overVerify']
        runner = self.runner()
        runner.p1_over_verify = '{"types":[1]}'
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.ad_details = AsyncMock(side_effect=lambda _: {'id': 'AD-SELL', 'coinName': 'USDT',
            'currency': 'RUB', 'tradeType': 1, 'availableQuantity': self.exchange.ad['availableQuantity'],
            'overVerify': {'types': [1]}})
        calls = 0
        async def replenish(plan):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AdsPowerUnavailable('Local API ReadTimeout')
            self.exchange.ad['availableQuantity'] = str(
                Decimal(self.exchange.ad['availableQuantity']) + Decimal(plan['quantity']))
        runner.browser.replenish_ad = AsyncMock(side_effect=replenish)
        with self.assertRaises(AdsPowerUnavailable):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_replenish')['status'], 'pending')
        self.assertEqual(self.exchange.ad['availableQuantity'], '9')
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(calls, 2)

    async def test_auto_resume_clicks_document_check_if_still_ready(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.approve.side_effect = [TimeoutError(), None]
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        await runner.run(self.cycle_id)
        self.assertEqual(runner.browser.approve.await_count, 2)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')

    async def test_auto_resume_does_not_click_for_changed_counterparty(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        runner.browser.approve.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        self.exchange.orders['ORDER-1']['userInfo'] = {'memberId': 'OUTSIDER', 'nickName': 'Trusted-P2'}
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(runner.browser.approve.await_count, 1)

    async def test_auto_resume_reconciles_timed_out_replenishment_without_duplicate(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        self.exchange.fail = 'replenish'
        with self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_replenish')['status'], 'unknown')
        self.assertEqual(len(self.exchange.ad_calls), 1)
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(len(self.exchange.ad_calls), 1)

    async def test_auto_resume_does_not_repeat_unconfirmed_replenishment(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        self.exchange.fail = 'replenish_no_change'
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_replenish')['status'], 'unknown')
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)

    async def test_explicit_timestamp_rejection_can_resume_without_duplicate_order(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        create = runner.clients['p2'].create_order
        attempts = 0
        async def reject_once(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise MexcAPIError('Timestamp outside recvWindow', code=700003, http_status=400)
            return await create(**kwargs)
        runner.clients['p2'].create_order = reject_once
        with self.assertRaises(MexcAPIError):
            await runner.run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, 'forward_create')
        self.assertEqual(saved['status'], 'rejected')
        self.assertEqual(saved['result']['rejected_code'], 700003)
        self.assertEqual(self.journal.db.execute('SELECT status FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT 1',
                                                (self.cycle_id,)).fetchone()[0], 'waiting')
        await runner.run(self.cycle_id)
        self.assertEqual(attempts, 3)  # Forward retry, then reverse creation.
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')

    async def test_http_504_during_order_creation_stays_unknown_and_is_not_retried(self):
        runner = self.runner()
        runner.delay_seconds = runner.delay_max_seconds = 0
        attempts = 0

        async def timeout(**kwargs):
            nonlocal attempts
            attempts += 1
            raise MexcAPIError('HTTP 504', code=504, http_status=504)

        runner.clients['p2'].create_order = timeout
        with self.assertRaises(MexcAPIError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_create')['status'], 'unknown')
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(attempts, 1)

    async def test_outsider_is_ignored_and_cycle_completes(self):
        runner = self.runner(); self.outsider()
        with patch("cycle.asyncio.sleep", new=AsyncMock()):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")
        self.assertEqual(self.exchange.orders["OUTSIDER"]["state"], "NOT_PAID")
        self.assertFalse(any(data == "OUTSIDER" for _, _, data in self.exchange.calls))
        runner.browser.approve.assert_awaited_once_with("ORDER-2")

    async def test_outsider_during_delay_does_not_stop_cycle(self):
        runner = self.runner()
        async def sleep(_): self.outsider()
        with patch("cycle.asyncio.sleep", side_effect=sleep):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")
        self.assertFalse(any(data == "OUTSIDER" for _, _, data in self.exchange.calls))

    async def test_own_order_identity_changes_before_click_blocks_approval(self):
        runner = self.runner()
        async def open_order(_):
            self.exchange.orders["ORDER-1"]["userInfo"] = {"memberId": "ANOTHER", "nickName": "Trusted-P2"}
            return "ready"
        runner.browser.open_order.side_effect = open_order
        with patch("cycle.asyncio.sleep", new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        runner.browser.approve.assert_not_called()
        self.assertFalse(any(op in {"paid", "release"} for _, op, _ in self.exchange.calls))

    async def test_unknown_create_is_never_repeated_automatically(self):
        runner = self.runner(); self.exchange.fail = "create"
        with patch("cycle.asyncio.sleep", new=AsyncMock()), self.assertRaises(TimeoutError):
            await runner.run(self.cycle_id)
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 1)

    async def test_unknown_participant_fails_closed(self):
        runner = self.runner()
        original = runner.clients['p2'].create_order
        async def create(**kwargs):
            order_no = await original(**kwargs)
            self.exchange.orders[order_no]['userInfo'] = {'nickName': 'Trusted-P2'}
            return order_no
        runner.clients['p2'].create_order = create
        with patch("cycle.asyncio.sleep", new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual([op for _, op, _ in self.exchange.calls], ['create'])
        runner.browser.approve.assert_not_called()
        runner.browser.close_order_tabs.assert_not_called()

    async def test_disabled_ad_verification_blocks_before_purchase(self):
        runner = self.runner()
        del self.exchange.ad['overVerify']
        runner.browser.ad_details = AsyncMock(return_value={'id': 'AD-SELL', 'overVerify': None})
        with self.assertRaises(ValueError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [])
        runner.browser.approve.assert_not_called()

    async def test_pending_server_verification_blocks_payment_and_repairs_old_check(self):
        runner = self.runner()
        # Simulate a historical false completion based on the page heading.
        runner.browser.inspect.return_value = "ready"
        with patch("cycle.asyncio.sleep", new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_check')['status'], 'done')
        self.assertFalse(any(op == 'paid' for _, op, _ in self.exchange.calls))
        async def real_approval(_):
            runner.browser.inspect.return_value = "passed"
        runner.browser.approve.side_effect = real_approval
        with patch("cycle.asyncio.sleep", new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        # Verification is repaired, but uncertain payment still requires interactive recovery.
        self.assertEqual(runner.browser.approve.await_count, 2)
        self.assertFalse(any(op == 'paid' for _, op, _ in self.exchange.calls))

    def test_delay_validation(self):
        self.assertEqual(action_delay("20"), 20)
        for value in ("nan", "inf", "-1", "3601"):
            with self.assertRaises(ValueError): action_delay(value)

    def test_delay_bounds_and_legacy_setting(self):
        self.assertEqual(delay_bounds({}), (20, 20))
        self.assertEqual(delay_bounds({'ACTION_DELAY_SECONDS': '7'}), (7, 7))
        self.assertEqual(delay_bounds({'ACTION_DELAY_MIN_SECONDS': '0.5', 'ACTION_DELAY_MAX_SECONDS': '30',
                                      'ACTION_DELAY_SECONDS': '9999'}), (0.5, 30))
        for low, high in [('30', '20'), ('', '20'), ('20', ''), ('nan', '20'), ('0', 'inf'), ('-1', '20')]:
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                delay_bounds({'ACTION_DELAY_MIN_SECONDS': low, 'ACTION_DELAY_MAX_SECONDS': high})

    async def test_random_delay_is_drawn_for_each_action(self):
        runner = self.runner()
        runner.delay_seconds, runner.delay_max_seconds = 10, 30
        count = sum(s.key.split('_', 1)[1] in MUTATING_ACTIONS for s in STEPS)
        pauses = [10 + i / 10 for i in range(count)]
        with patch('cycle.random.uniform', side_effect=pauses) as draw, patch('cycle.asyncio.sleep', new=AsyncMock()) as sleep:
            await runner.run(self.cycle_id)
        self.assertEqual(draw.call_count, count)
        self.assertTrue(all(c.args == (10, 30) for c in draw.call_args_list))
        self.assertAlmostEqual(sum(c.args[0] for c in sleep.call_args_list), sum(pauses))

    def test_id_and_nickname_are_both_required_exactly(self):
        runner = self.runner()
        for member, nickname in [('OTHER', 'Trusted-P2'), ('MEMBER-P2', 'other'),
                                 ('MEMBER-P2', 'trusted-p2'), ('MEMBER-P2', 'Trusted-P2 '),
                                 (None, 'Trusted-P2'), ('MEMBER-P2', None), ('MEMBER-P2', '')]:
            with self.subTest(member=member, nickname=nickname), self.assertRaises(Paused):
                runner.check_counterparty({'userInfo': {'memberId': member, 'nickName': nickname}}, 'p1', 'ORDER')
        runner.check_counterparty({'userInfo': {'memberId': 'MEMBER-P2', 'nickName': 'Trusted-P2'}}, 'p1', 'ORDER')
        # P1 nickname is optional by operator request, but its ID remains mandatory.
        runner.check_counterparty({'merchantInfo': {'memberId': 'MEMBER-P1'}}, 'p2', 'ORDER')
        with self.assertRaises(Paused):
            runner.check_counterparty({'merchantInfo': {'memberId': 'OTHER'}}, 'p2', 'ORDER')

    async def test_wrong_nickname_on_entry_blocks_before_chat_or_approval(self):
        runner = self.runner()
        original = runner.clients['p1'].get_order_detail
        async def detail(order_no):
            data = await original(order_no)
            data['userInfo']['nickName'] = 'OTHER'
            return data
        runner.clients['p1'].get_order_detail = detail
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual([op for _, op, _ in self.exchange.calls], ['create'])
        runner.browser.approve.assert_not_called()

    async def assert_release_guard(self, leg, actor, field, value):
        runner = self.runner()
        original_execute = runner.execute
        original_detail = runner.clients[actor].get_order_detail
        async def detail(order_no):
            data = await original_detail(order_no)
            data['userInfo'][field] = value
            return data
        async def execute(step, data, *, recovery):
            # Change identity after preparation/confirmation, at the last action boundary.
            if step.key == leg + '_release':
                runner.clients[actor].get_order_detail = detail
            return await original_execute(step, data, recovery=recovery)
        runner.execute = execute
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        blocked_order = 'ORDER-1' if leg == 'forward' else 'ORDER-2'
        self.assertFalse(any(op == 'release' and data == blocked_order for _, op, data in self.exchange.calls))
        self.assertIn('Действие заблокировано', self.telegram.messages[-1])

    async def test_forward_release_rechecks_p2_nickname(self):
        await self.assert_release_guard('forward', 'p1', 'nickName', 'OTHER')

    async def test_reverse_release_rechecks_p2_id_even_if_nickname_matches(self):
        await self.assert_release_guard('reverse', 'p1', 'memberId', 'OTHER')

    async def test_reverse_release_rechecks_p1_id(self):
        await self.assert_release_guard('reverse', 'p2', 'memberId', 'OTHER')

    async def test_action_cannot_target_unrelated_order_number(self):
        runner = self.runner()
        original = runner.prepare
        async def prepare(step, *, recovery):
            data = await original(step, recovery=recovery)
            if step.key == 'forward_release':
                data['order_no'] = 'OUTSIDER'
            return data
        runner.prepare = prepare
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertFalse(any(op == 'release' for _, op, _ in self.exchange.calls))

    async def test_missing_p2_nickname_blocks_before_creation(self):
        runner = self.runner()
        runner.trusted_nicknames = {}
        with self.assertRaises(ValueError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [])

    async def test_tab_cleanup_failure_does_not_replay_trades(self):
        runner = self.runner()
        runner.browser.close_order_tabs.side_effect = TimeoutError()
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await runner.run(self.cycle_id)
        calls = list(self.exchange.calls)
        await runner.run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'completed')
        self.assertEqual(self.exchange.calls, calls)

    async def test_repeated_security_checks_only_print_changed_order_state(self):
        runner = self.runner()
        output = []
        runner.console.write = output.append
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await runner.run(self.cycle_id)
        summaries = [line for line in output if line.startswith(('Первая сделка:', 'Обратная сделка:'))]
        self.assertEqual(len(summaries), 6)  # Three states per leg, not every poll/account.
        before = len(output)
        await runner.snapshot('reverse')
        self.assertEqual(len(output), before)
        self.exchange.orders['ORDER-2']['userInfo'] = {'memberId': 'OTHER', 'nickName': 'Trusted-P2'}
        with self.assertRaises(Paused):
            await runner.snapshot('reverse')  # No cached trust despite unchanged amount/status.
