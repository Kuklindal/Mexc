import asyncio
import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from cycle import Console, CycleRunner, Paused, money, new_spec, purchase_amount
from journal import Journal, process_lock
from mexc_client import MexcAPIError, ad_replenish_params
from sheets import GoogleSheets, Reporter


class Telegram:
    enabled = True

    def __init__(self):
        self.messages = []
        self.fail = False

    async def send(self, text):
        self.messages.append(text)
        return not self.fail


class Sheet:
    def __init__(self):
        self.rows = {}
        self.fail_once = False

    async def prepare(self, journal):
        pass

    async def send(self, sale):
        self.rows[sale['id']] = sale['amount']
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("Response lost after write")


class Operator(Console):
    def __init__(self):
        super().__init__(write=lambda text: None)
        self.confirmations = []
        self.pause_on = ""
        self.reverse = False
        self.auth = "NONE"

    def confirm(self, prompt, phrase="ДА"):
        self.confirmations.append((prompt, phrase))
        if self.pause_on and self.pause_on in phrase:
            raise Paused("Test pause")

    def ask(self, prompt, default="", validate=None):
        if "advNo" in prompt:
            value = "AD-BUY" if self.reverse else "AD-SELL"
            self.reverse = True
        elif "уже открытого" in prompt:
            value = "ORDER-1"
        elif "ID " in prompt:
            value = "123"
        elif "Текст" in prompt:
            value = "Здравствуйте"
        elif "Проверка MEXC" in prompt:
            value = self.auth
        else:
            value = default
        return validate(value) if validate else value


class Exchange:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.fail = ""
        self.wrong_amount = False
        self.payment_ids = []
        self.ignore_paid = False
        self.ignore_release = False
        self.p2_state = None
        self.completed_state = "DONE"
        self.reverse_initial_state = "PROCESSING"
        self.ad_calls = []
        self.ad = {"advNo": "AD-SELL", "side": "SELL", "coinName": "USDT", "coinId": "USDT-ID",
                   "fiatUnit": "RUB", "quantity": "10000", "availableQuantity": "9", "price": "100",
                   "payTimeLimit": 15, "minSingleTransAmount": "100", "maxSingleTransAmount": "10000",
                   "userAllTradeCountMin": 0, "userAllTradeCountMax": 0, "advStatus": "CLOSE",
                   "paymentInfo": [{"id": 2604184, "payMethod": 578}], "tradeTerms": "Cash",
                   "overVerify": {"types": [1]},
                   "onlyTradeKybUser": False}

    def client(self, actor):
        exchange = self

        class Client:
            async def get_ad(self, adv_no):
                assert actor == "p1"
                ad = copy.deepcopy(exchange.ad)
                if adv_no == "AD-BUY":
                    ad.update(advNo=adv_no, side="BUY")
                return ad

            async def list_active_maker_orders(self):
                return [d.copy() for d in exchange.orders.values() if d['state'] not in {'DONE', 'COMPLETED', 'CANCEL'}]

            async def replenish_ad(self, ad, quantity):
                assert actor == "p1"
                exchange.ad_calls.append(ad_replenish_params(ad, quantity))
                if exchange.fail == "replenish_signature":
                    exchange.fail = ""
                    raise MexcAPIError("Invalid signature", code=700002, http_status=400)
                if exchange.fail == "replenish_limit":
                    exchange.fail = ""
                    raise MexcAPIError("Quantity limit", code=60048, http_status=200)
                if exchange.fail == "replenish_order_limit":
                    exchange.fail = ""
                    raise MexcAPIError("Order limit", code=60064, http_status=200)
                if exchange.fail == "replenish_no_change":
                    return
                for key in ("quantity", "availableQuantity"):
                    exchange.ad[key] = str(Decimal(exchange.ad[key]) + Decimal(quantity))
                if exchange.fail != "ignore_max_limit":
                    exchange.ad["maxSingleTransAmount"] = exchange.ad_calls[-1]["maxSingleTransAmount"]
                if exchange.fail == "replenish":
                    exchange.fail = ""
                    raise TimeoutError()

            async def create_order(self, **kwargs):
                exchange.calls.append((actor, "create", kwargs))
                if exchange.fail == "reverse_create" and "tradable_quantity" in kwargs:
                    exchange.fail = ""
                    raise RuntimeError("Collection/payment method rejected")
                number = f"ORDER-{len(exchange.orders) + 1}"
                exchange.orders[number] = {"advOrderNo": number, "advNo": kwargs['adv_no'],
                    "coinName": "USDT", "fiatUnit": "RUB",
                    "state": exchange.reverse_initial_state if "tradable_quantity" in kwargs else "NOT_PAID",
                    "amount": "10001" if exchange.wrong_amount else kwargs.get("amount", "9950"),
                    "paymentInfo": [{"id": 2604184, "payMethod": 578}],
                    "tradableQuantity": kwargs.get("tradable_quantity", "100")}
                if exchange.fail == "create":
                    exchange.fail = ""
                    raise TimeoutError()
                return number

            async def get_order_detail(self, order_no):
                detail = exchange.orders[order_no].copy()
                detail.setdefault("userInfo", {"memberId": "MEMBER-P2" if actor == "p1" else "MEMBER-P1",
                                               "nickName": "Trusted-P2" if actor == "p1" else "Trusted-P1"})
                if actor == "p2" and exchange.p2_state:
                    detail["state"] = exchange.p2_state
                return detail

            async def send_chat_text(self, order_no, text):
                exchange.calls.append((actor, "chat", order_no))

            async def mark_chat_read(self, order_no):
                return False

            async def mark_paid(self, order_no, payment_account_id):
                exchange.payment_ids.append(payment_account_id)
                exchange.calls.append((actor, "paid", order_no))
                if not exchange.ignore_paid:
                    exchange.orders[order_no]["state"] = "PAID"

            async def release_coin(self, order_no, **kwargs):
                exchange.calls.append((actor, "release", order_no))
                if not exchange.ignore_release:
                    exchange.orders[order_no]["state"] = exchange.completed_state
                if exchange.fail == "release":
                    exchange.fail = ""
                    raise TimeoutError()

        return Client()


class CycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite3"
        self.journal = Journal(self.path)
        self.telegram, self.sheet = Telegram(), Sheet()
        self.reporter = Reporter(self.journal, self.telegram, self.sheet)
        self.exchange, self.operator = Exchange(), Operator()
        self.spec = {"mode": "api", "amount": "10000", "fiat": "RUB", "profiles": {}}
        self.cycle_id = self.journal.create(self.spec)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def runner(self, enabled=True):
        return CycleRunner(self.journal, self.reporter,
            {actor: self.exchange.client(actor) for actor in ("p1", "p2")},
            self.operator, state_changes=enabled)

    async def test_full_cycle_roles_amount_and_no_reexecution(self):
        await self.runner().run(self.cycle_id)
        self.assertEqual([(a, op) for a, op, _ in self.exchange.calls], [
            ("p2", "create"), ("p2", "chat"), ("p1", "chat"), ("p2", "paid"), ("p1", "release"),
            ("p2", "create"), ("p2", "chat"), ("p1", "chat"), ("p1", "paid"), ("p2", "release")])
        self.assertEqual(self.exchange.calls[5][2]["tradable_quantity"], "100")
        self.assertEqual(self.exchange.payment_ids, [2604184, 2604184])
        self.assertEqual(list(self.sheet.rows.values()), ["10000"])
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")
        self.assertTrue(any("ПРОВЕРКА ПРОЙДЕНА" in phrase for _, phrase in self.operator.confirmations))
        self.assertEqual(self.telegram.messages, [])
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.calls), 10)

    async def test_pause_before_release_and_resume_from_disk(self):
        self.operator.pause_on = "ДЕНЬГИ ПОЛУЧЕНЫ ORDER-1"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertFalse(any(op == "release" for _, op, _ in self.exchange.calls))
        self.journal.close()
        self.journal = Journal(self.path)
        self.reporter.journal = self.journal
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 2)
        self.assertEqual(sum(op == "paid" for _, op, _ in self.exchange.calls), 2)

    async def test_ambiguous_create_never_retried(self):
        self.exchange.fail = "create"
        with self.assertRaises(TimeoutError):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, "forward_create")["status"], "unknown")
        await self.runner().run(self.cycle_id)
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 2)
        self.assertTrue(any(phrase == "СВЕРЕНО forward_create" for _, phrase in self.operator.confirmations))

    async def test_ambiguous_release_never_retried(self):
        self.exchange.fail = "release"
        with self.assertRaises(TimeoutError):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.orders["ORDER-1"]["state"], "DONE")
        await self.runner().run(self.cycle_id)
        self.assertEqual(sum(op == "release" for _, op, _ in self.exchange.calls), 2)
        self.assertEqual(self.exchange.calls.count(("p1", "release", "ORDER-1")), 1)
        self.assertEqual(self.exchange.calls.count(("p2", "paid", "ORDER-1")), 1)
        self.assertEqual(self.exchange.calls[5], ("p2", "create", {
            "adv_no": "AD-BUY", "tradable_quantity": "100", "user_confirm_payment_id": 123}))
        self.assertEqual(list(self.sheet.rows.values()), ["10000"])
        self.assertIn("СВЕРЕНО forward_release", [phrase for _, phrase in self.operator.confirmations])

    async def test_completed_alias_still_supported(self):
        self.exchange.completed_state = "COMPLETED"
        await self.runner().run(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")

    async def test_browser_check_waits_for_operator_and_does_not_repeat_unknown_click(self):
        browser = type("Browser", (), {})()
        browser.inspect = AsyncMock(return_value="ready")
        browser.open_order = AsyncMock(return_value="ready")
        browser.approve = AsyncMock(side_effect=RuntimeError("UI result unknown"))
        runner = self.runner()
        runner.browser = browser
        self.operator.pause_on = "ДОКУМЕНТЫ ПРОВЕРЕНЫ ORDER-1"
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        browser.approve.assert_not_called()
        self.operator.pause_on = ""
        with self.assertRaises(RuntimeError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, "forward_check")["status"], "unknown")
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.assertEqual(browser.approve.await_count, 1)
        browser.inspect.return_value = "passed"
        await runner.run(self.cycle_id)
        self.assertEqual(browser.approve.await_count, 1)

    async def test_console_cycle_browser_confirmation_and_resume(self):
        browser = type("Browser", (), {})()
        browser.open_order = AsyncMock(return_value="ready")
        browser.inspect = AsyncMock(return_value="passed")
        browser.approve = AsyncMock()
        runner = self.runner()
        runner.browser = browser
        original_confirm = self.operator.confirm
        def confirm(prompt, phrase="ДА"):
            if phrase == "ДА" and '"verification": "adspower"' in prompt:
                raise Paused("Stop before browser click")
            original_confirm(prompt, phrase)
        self.operator.confirm = confirm
        with self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        browser.open_order.assert_awaited_once_with("ORDER-1")
        browser.approve.assert_not_called()
        self.assertFalse(any(op in {"paid", "release"} for _, op, _ in self.exchange.calls))
        self.operator.confirm = original_confirm
        await runner.run(self.cycle_id)
        browser.approve.assert_awaited_once_with("ORDER-1")
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")
        self.assertEqual(len(self.exchange.calls), 10)
        self.assertEqual(self.exchange.ad["availableQuantity"], "109")
        await runner.run(self.cycle_id)
        browser.approve.assert_awaited_once()
        self.assertEqual(len(self.exchange.calls), 10)

    async def test_replenish_increments_original_ad_once_and_preserves_settings(self):
        await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.ad["availableQuantity"], "109")
        self.assertEqual(self.exchange.ad["quantity"], "10100")
        params = self.exchange.ad_calls[0]
        self.assertEqual(params["advNo"], "AD-SELL")
        self.assertEqual(params["initQuantity"], "109")
        self.assertEqual(params["supplyQuantity"], "100")
        self.assertEqual(params["payMethod"], "2604184")
        self.assertEqual(params["price"], "100")
        self.assertEqual(params["tradeTerms"], "Cash")
        self.assertFalse(params["onlyTradeKybUser"])
        self.assertNotIn("advStatus", params)
        self.assertEqual(self.exchange.ad["advStatus"], "CLOSE")
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.assertEqual(list(self.sheet.rows.values()), ["10000"])
        self.assertFalse(any("Пополнение объявления: +100 USDT" in message for message in self.telegram.messages))

    async def test_replenish_timeout_is_reconciled_without_second_increment(self):
        self.exchange.fail = "replenish"
        with self.assertRaises(TimeoutError):
            await self.runner().run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, "reverse_replenish")
        self.assertEqual(saved["status"], "unknown")
        self.assertEqual(saved["result"]["quantity"], "100")
        self.assertEqual(saved["result"]["target_available"], "109")
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.assertIn("СВЕРЕНО reverse_replenish", [p for _, p in self.operator.confirmations])

    async def test_signature_rejection_retries_only_replenish_after_confirmation(self):
        self.exchange.fail = "replenish_signature"
        with self.assertRaises(MexcAPIError):
            await self.runner().run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, "reverse_replenish")
        self.assertEqual(saved["result"]["rejected_code"], 700002)
        self.operator.pause_on = "ПОВТОРИТЬ reverse_replenish"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 2)
        self.assertEqual(len(self.exchange.calls), 10)
        self.assertEqual(self.exchange.ad["availableQuantity"], "109")
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")

    async def test_signature_rejection_cannot_retry_if_ad_changed(self):
        self.exchange.fail = "replenish_signature"
        with self.assertRaises(MexcAPIError):
            await self.runner().run(self.cycle_id)
        self.exchange.ad["availableQuantity"] = "8"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)

    async def test_signature_rejection_with_target_already_reached_does_not_add_again(self):
        self.exchange.fail = "replenish_signature"
        with self.assertRaises(MexcAPIError):
            await self.runner().run(self.cycle_id)
        self.exchange.ad["availableQuantity"] = "109"
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)

    async def test_quantity_limit_rejection_requires_confirmation_before_retry(self):
        self.exchange.fail = "replenish_limit"
        with self.assertRaises(MexcAPIError):
            await self.runner().run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, "reverse_replenish")
        self.assertEqual(saved["result"]["rejected_code"], 60048)
        self.operator.pause_on = "ПОВТОРИТЬ reverse_replenish"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 2)
        self.assertEqual(len(self.exchange.calls), 10)
        self.assertEqual(self.exchange.ad["availableQuantity"], "109")

    def test_historical_quantity_is_not_sent_as_initial_quantity(self):
        for available, increment, expected in [("3882.4123", "107.5877", "3990.0000"),
                                               ("7.3051", "101.6949", "109.0000"),
                                               ("0", "100", "100")]:
            with self.subTest(available=available):
                ad = self.exchange.ad | {"quantity": "3610563.5595", "availableQuantity": available}
                params = ad_replenish_params(ad, increment)
                self.assertEqual(params["initQuantity"], expected)
                self.assertEqual(params["supplyQuantity"], increment)

    def test_invalid_replenish_amounts_cannot_be_sent(self):
        for available, increment in [("-1", "100"), ("NaN", "100"), ("1", "Infinity"),
                                     ("1", "0"), ("1", "-10"), ("bad", "100")]:
            with self.subTest(available=available, increment=increment), self.assertRaises(ValueError):
                ad_replenish_params(self.exchange.ad | {"availableQuantity": available}, increment)

    def test_order_limit_is_capped_to_target_value_and_minimum_is_preserved(self):
        ad = self.exchange.ad | {"availableQuantity": "3882.4123", "price": "88.3",
             "maxSingleTransAmount": "500600", "minSingleTransAmount": "8888.88"}
        params = ad_replenish_params(ad, "107.5877")
        self.assertEqual(params["initQuantity"], "3990.0000")
        self.assertEqual(params["supplyQuantity"], "107.5877")
        self.assertEqual(params["maxSingleTransAmount"], "352317.00")
        self.assertEqual(params["minSingleTransAmount"], "8888.88")
        self.assertEqual(ad["maxSingleTransAmount"], "500600")
        with self.assertRaises(ValueError):
            ad_replenish_params(ad | {"minSingleTransAmount": "400000"}, "107.5877")

    async def test_order_limit_retry_shows_change_and_requires_confirmation(self):
        self.exchange.ad["maxSingleTransAmount"] = "20000"
        self.exchange.fail = "replenish_order_limit"
        with self.assertRaises(MexcAPIError):
            await self.runner().run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, "reverse_replenish")["result"]
        self.assertEqual(saved["rejected_code"], 60064)
        self.assertEqual(saved["before_max_limit"], "20000")
        self.assertEqual(saved["target_max_limit"], "10900.00")
        self.operator.pause_on = "ПОВТОРИТЬ reverse_replenish"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.assertEqual(self.exchange.ad["maxSingleTransAmount"], "20000")
        prompt, phrase = self.operator.confirmations[-1]
        self.assertIn('"before_max_limit": "20000"', prompt)
        self.assertIn('"target_max_limit": "10900.00"', prompt)
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.ad["maxSingleTransAmount"], "10900.00")
        self.assertEqual(len(self.exchange.calls), 10)

    async def test_applied_volume_with_wrong_order_limit_is_not_completed_or_retried(self):
        self.exchange.ad["maxSingleTransAmount"] = "20000"
        self.exchange.fail = "ignore_max_limit"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)

    def test_edit_to_original_limit_changes_replenishment_plan_even_if_cap_is_same(self):
        runner = self.runner(); runner.spec = self.spec
        first = runner.replenish_plan(self.exchange.ad | {"maxSingleTransAmount": "20000"}, "AD-SELL", "100")
        second = runner.replenish_plan(self.exchange.ad | {"maxSingleTransAmount": "30000"}, "AD-SELL", "100")
        self.assertEqual(first["target_max_limit"], second["target_max_limit"])
        self.assertNotEqual(first, second)

    async def test_replenish_unchanged_volume_stops_without_automatic_retry(self):
        self.exchange.fail = "replenish_no_change"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "paused")

    async def test_replenish_detects_edit_during_confirmation(self):
        confirm = self.operator.confirm
        def change_ad(prompt, phrase="ДА"):
            confirm(prompt, phrase)
            if '"before_available"' in prompt:
                self.exchange.ad["price"] = "101"
        self.operator.confirm = change_ad
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.ad_calls, [])

    def test_replenish_targets_available_not_cumulative_quantity(self):
        runner = self.runner()
        runner.spec = self.spec
        ad = self.exchange.ad | {"quantity": "3422113.3163", "availableQuantity": "7.3051"}
        plan = runner.replenish_plan(ad, "AD-SELL", "101.6949")
        self.assertEqual(Decimal(plan["target_available"]), Decimal("109"))
        self.assertNotIn("target_quantity", plan)
        # A growing cumulative total is not enough if available funds did not grow.
        with self.assertRaises(Paused):
            runner.check_replenished(ad | {"quantity": "3422215.0112"}, plan)
        runner.check_replenished(ad | {"availableQuantity": "109"}, plan)

    async def test_legacy_replenish_intent_uses_original_available_without_repeating(self):
        self.exchange.fail = "replenish"
        with self.assertRaises(TimeoutError):
            await self.runner().run(self.cycle_id)
        saved = self.journal.step(self.cycle_id, "reverse_replenish")["result"]
        saved.pop("target_available")
        saved.update(before_quantity="10000", target_quantity="10100")
        self.journal.transition(self.cycle_id, "reverse_replenish", "p1", "unknown", "Legacy intent", result=saved)
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        result = self.journal.step(self.cycle_id, "reverse_replenish")["result"]
        self.assertEqual(result["target_available"], "109")
        self.assertNotIn("target_quantity", result)

    async def test_old_completed_cycle_can_resume_only_missing_replenishment(self):
        self.operator.pause_on = "ЗАВЕРШЕНО ORDER-2"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.journal.transition(self.cycle_id, "reverse_complete", "both", "done", "Old completion",
                                result={"quantity": "100"}, cycle_status="completed")
        calls = list(self.exchange.calls)
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls, calls)
        self.assertEqual(len(self.exchange.ad_calls), 1)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")

    async def test_configured_p2_payment_account_skips_prompt_but_requires_confirmation(self):
        original = self.operator.ask
        def ask(prompt, default="", validate=None):
            if "ID платёжных реквизитов П2" in prompt:
                self.fail("Configured receiving account must not be requested again")
            return original(prompt, default, validate)
        self.operator.ask = ask
        runner = self.runner()
        runner.p2_payment_id = "9876543"
        await runner.run(self.cycle_id)
        self.assertEqual(self.exchange.calls[5][2]["user_confirm_payment_id"], 9876543)
        self.assertEqual(self.exchange.calls[0][2]["user_confirm_pay_method_id"], 123)
        self.assertTrue(any('"user_confirm_payment_id": 9876543' in text and phrase == "ДА"
                            for text, phrase in self.operator.confirmations))

    async def test_invalid_configured_payment_id_stops_before_reverse_create(self):
        runner = self.runner()
        runner.p2_payment_id = "not-an-id"
        with self.assertRaises(ValueError):
            await runner.run(self.cycle_id)
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 1)

    async def test_rejected_reverse_creation_requires_checked_absence_and_explicit_retry(self):
        self.exchange.fail = "reverse_create"
        with self.assertRaises(RuntimeError):
            await self.runner().run(self.cycle_id)
        original = self.operator.ask
        self.operator.ask = lambda prompt, default="", validate=None: (
            "НЕ СОЗДАН" if prompt == "Результат предыдущей попытки"
            else original(prompt, default, validate))
        self.operator.pause_on = "ОРДЕР НЕ СОЗДАН reverse_create"
        calls_before = list(self.exchange.calls)
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls, calls_before)
        self.assertEqual(self.journal.step(self.cycle_id, "reverse_create")["status"], "unknown")
        self.operator.pause_on = "ПОВТОРИТЬ reverse_create"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls, calls_before)
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.orders), 2)
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 3)
        self.assertEqual(self.exchange.calls.count(("p2", "paid", "ORDER-1")), 1)
        self.assertEqual(self.exchange.calls.count(("p1", "release", "ORDER-1")), 1)
        self.assertEqual(list(self.sheet.rows.values()), ["10000"])
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "completed")

    async def test_read_only_gate(self):
        with self.assertRaises(ValueError):
            await self.runner(enabled=False).run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [])

    async def test_existing_ad_requires_confirmation_before_creating_order(self):
        self.spec["forward_adv_no"] = "READY-AD-123"
        self.journal.db.execute("UPDATE cycles SET spec=? WHERE id=?",
            (__import__('json').dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        # Confirm the existing ad, enter payment method, stop before creating an order.
        answers = iter(["ДА", "123", "STOP"])
        self.operator = Console(read=lambda _: next(answers), write=lambda _: None)
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [])
        # Resume: the saved ad is reused. Confirm order creation, stop at order review.
        answers = iter(["123", "ДА", "STOP"])
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [("p2", "create", {
            "adv_no": "READY-AD-123", "amount": "10000", "user_confirm_pay_method_id": 123})])

    async def test_wrong_amount_stops_before_chat_or_payment(self):
        self.exchange.wrong_amount = True
        with self.assertRaises(ValueError):
            await self.runner().run(self.cycle_id)
        self.assertEqual(len(self.exchange.calls), 1)

    async def test_payment_method_is_not_used_as_account_id(self):
        self.exchange.orders["ORDER-TEST"] = {"advOrderNo": "ORDER-TEST", "paymentInfo": [{"payMethod": 578}]}
        with self.assertRaises(ValueError):
            await self.runner().payment_account("p2", "ORDER-TEST")
        self.assertEqual(self.exchange.payment_ids, [])

    async def test_paid_success_without_remote_state_change_blocks_release(self):
        self.exchange.ignore_paid = True
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, "forward_paid")["status"], "unknown")
        self.assertFalse(any(op == "release" for _, op, _ in self.exchange.calls))
        self.assertEqual(self.sheet.rows, {})
        # Resume must explicitly authorize retrying the API call; it cannot accept a fake 'verified'.
        self.exchange.ignore_paid = False
        await self.runner().run(self.cycle_id)
        self.assertIn("ПОВТОРИТЬ forward_paid", [phrase for _, phrase in self.operator.confirmations])
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 2)

    async def test_release_success_without_completion_does_not_record_sale(self):
        self.exchange.ignore_release = True
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, "forward_release")["status"], "unknown")
        self.assertIsNone(self.journal.step(self.cycle_id, "forward_complete"))
        self.assertEqual(self.sheet.rows, {})

    async def test_reverse_processing_requires_paid_transition_before_release(self):
        self.operator.pause_on = "ОПЛАТА ВЫПОЛНЕНА ORDER-2"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.orders["ORDER-2"]["state"], "PROCESSING")
        self.assertNotIn(("p1", "paid", "ORDER-2"), self.exchange.calls)
        self.operator.pause_on = ""
        self.exchange.ignore_paid = True
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, "reverse_paid")["status"], "unknown")
        self.assertNotIn(("p2", "release", "ORDER-2"), self.exchange.calls)
        self.assertNotIn("СВЕРЕНО reverse_paid", [phrase for _, phrase in self.operator.confirmations])
        self.exchange.ignore_paid = False
        await self.runner().run(self.cycle_id)
        self.assertIn("ПОВТОРИТЬ reverse_paid", [phrase for _, phrase in self.operator.confirmations])
        self.assertEqual(self.exchange.calls.count(("p2", "release", "ORDER-2")), 1)
        self.assertEqual(self.exchange.calls.count(("p1", "release", "ORDER-1")), 1)
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 2)

    async def test_reverse_waiting_or_cancelled_cannot_be_marked_paid(self):
        self.operator.pause_on = "ОПЛАТА ВЫПОЛНЕНА ORDER-2"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.operator.pause_on = ""
        calls = list(self.exchange.calls)
        for state in ("WAIT_PROCESS", "CANCEL", "TIMEOUT", "INVALID", "REFUSE", "UNKNOWN"):
            with self.subTest(state=state):
                self.exchange.orders["ORDER-2"]["state"] = state
                with self.assertRaises(Paused):
                    await self.runner().run(self.cycle_id)
                self.assertEqual(self.exchange.calls, calls)

    async def test_payment_marked_on_site_is_verified_without_duplicate_api_call(self):
        self.operator.pause_on = "ОПЛАТА ВЫПОЛНЕНА ORDER-1"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.exchange.orders["ORDER-1"]["state"] = "PAID"
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertNotIn(("p2", "paid", "ORDER-1"), self.exchange.calls)
        self.assertIn("СВЕРЕНО forward_paid", [phrase for _, phrase in self.operator.confirmations])

    async def test_legacy_false_paid_ack_is_repaired_before_release(self):
        self.operator.pause_on = "ДЕНЬГИ ПОЛУЧЕНЫ ORDER-1"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.exchange.orders["ORDER-1"]["state"] = "NOT_PAID"
        self.journal.transition(self.cycle_id, "forward_release", "p1", "unknown", "Legacy release rejected")
        self.exchange.calls.clear()
        self.operator.pause_on = ""
        await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls[0], ("p2", "paid", "ORDER-1"))
        self.assertEqual(self.exchange.calls[1], ("p1", "release", "ORDER-1"))
        self.assertIn("ПОВТОРИТЬ forward_release", [phrase for _, phrase in self.operator.confirmations])
        self.assertEqual(sum(op == "create" for _, op, _ in self.exchange.calls), 1)

    async def test_release_is_blocked_if_state_changes_during_confirmation(self):
        confirm = self.operator.confirm
        def change_state(prompt, phrase="ДА"):
            confirm(prompt, phrase)
            if phrase == "ДЕНЬГИ ПОЛУЧЕНЫ ORDER-1":
                self.exchange.orders["ORDER-1"]["state"] = "NOT_PAID"
        self.operator.confirm = change_state
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertFalse(any(op == "release" for _, op, _ in self.exchange.calls))

    async def test_different_account_states_block_release(self):
        self.operator.pause_on = "ДЕНЬГИ ПОЛУЧЕНЫ ORDER-1"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.operator.pause_on = ""
        self.exchange.p2_state = "NOT_PAID"
        with self.assertRaises(Paused):
            await self.runner().run(self.cycle_id)
        self.assertFalse(any(op == "release" for _, op, _ in self.exchange.calls))

    async def test_multiple_payment_accounts_require_valid_selection(self):
        self.exchange.orders["ORDER-TEST"] = {"advOrderNo": "ORDER-TEST", "paymentInfo": [
            {"id": 2604184, "payMethod": 578}, {"id": 9876543, "payMethod": 578}]}
        answers = iter(["578", "2"])
        self.operator = Console(read=lambda _: next(answers), write=lambda _: None)
        self.assertEqual(await self.runner().payment_account("p2", "ORDER-TEST"), 9876543)

    async def test_codes_not_in_journal_or_telegram(self):
        self.operator.auth = "GA"
        with patch("cycle.getpass", return_value="SENSITIVE_CODE_123"):
            await self.runner().run(self.cycle_id)
        dump = "\n".join(self.journal.db.iterdump())
        self.assertNotIn("SENSITIVE_CODE_123", dump)
        self.assertNotIn("SENSITIVE_CODE_123", "\n".join(self.telegram.messages))

    async def test_delivery_failure_does_not_repeat_trade(self):
        self.telegram.fail = True
        self.sheet.fail_once = True
        await self.runner().run(self.cycle_id)
        self.assertTrue(self.journal.pending("telegram"))
        self.telegram.fail = False
        await self.reporter.flush()
        self.assertFalse(self.journal.pending("telegram"))
        self.assertFalse(self.journal.pending_sales())
        self.assertEqual(list(self.sheet.rows.values()), ["10000"])
        self.assertEqual(len(self.exchange.calls), 10)

    async def test_manual_mode_does_not_call_exchange(self):
        self.journal.db.execute("UPDATE cycles SET spec=? WHERE id=?",
            (__import__('json').dumps(self.spec | {"mode": "manual"}), self.cycle_id))
        self.journal.db.commit()
        original = self.operator.ask
        orders = iter(["ORDER-1", "ORDER-2"])

        def ask(prompt, default="", validate=None):
            if "уже открытого" in prompt:
                return next(orders)
            if "Фактическое количество" in prompt:
                return "100"
            if "Фактическая сумма" in prompt:
                return default or "9950"
            return original(prompt, default, validate)

        self.operator.ask = ask
        await self.runner(enabled=False).run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [])
        self.assertEqual(list(self.sheet.rows.values()), ["10000"])

    def test_second_active_cycle_rejected(self):
        with self.assertRaises(RuntimeError):
            self.journal.create(self.spec)

    async def test_reset_preserves_history_and_sales_but_blocks_resume(self):
        self.journal.transition(self.cycle_id, "forward_create", "p2", "done", "Created",
                                result={"order_no": "OLD-ORDER"})
        self.journal.transition(self.cycle_id, "forward_complete", "both", "done", "First sale",
                                context={"amount": "10000"})
        self.journal.abandon(self.cycle_id)
        self.assertEqual(self.journal.cycle(self.cycle_id)["status"], "abandoned")
        self.assertEqual(self.journal.step(self.cycle_id, "forward_create")["result"]["order_no"], "OLD-ORDER")
        self.assertEqual(self.journal.pending_sales()[0]["amount"], "10000")
        self.assertTrue(self.journal.pending("telegram"))
        self.assertNotEqual(self.journal.create(self.spec), self.cycle_id)
        with self.assertRaises(ValueError):
            await self.runner().run(self.cycle_id)
        self.assertEqual(self.exchange.calls, [])


class InputTests(unittest.TestCase):
    def test_purchase_amount_currency_validation(self):
        self.assertEqual(purchase_amount("1000 rub"), ("1000", "RUB"))
        self.assertEqual(purchase_amount("1000"), ("1000", "RUB"))
        self.assertEqual(purchase_amount("100,50 KZT"), ("100.50", "KZT"))
        for value in ("1 100", "1000 USDT", "1000 РУБ", "0 RUB", "NaN RUB"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                purchase_amount(value)

    def test_new_cycle_reprompts_bad_currency_then_asks_existing_ad(self):
        answers = iter(["1 100", "1000 RUB", "READY-AD-123"])
        messages = []
        spec = new_spec(Console(read=lambda _: next(answers), write=messages.append), "api", {})
        self.assertEqual(spec["amount"], "1000")
        self.assertEqual(spec["fiat"], "RUB")
        self.assertEqual(spec["forward_adv_no"], "READY-AD-123")
        self.assertTrue(any("три латинские буквы" in message for message in messages))

    def test_nonfinite_and_nonpositive_money(self):
        for value in ("NaN", "Infinity", "-1", "0", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        self.assertEqual(Decimal(money("123,45")), Decimal("123.45"))

    def test_enter_is_not_confirmation(self):
        values = iter(["", "нет"])
        console = Console(read=lambda _: next(values), write=lambda _: None)
        with self.assertRaises(Paused):
            console.confirm("Execute?")

    def test_process_lock_blocks_second_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with process_lock(path):
                with self.assertRaises(RuntimeError):
                    with process_lock(path):
                        self.fail("Concurrent writer accepted")


class SheetsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_sale_amount_and_retry_same_cell(self):
        sheets = GoogleSheets.__new__(GoogleSheets)
        calls = []

        async def request(method, cell_range, values=None):
            calls.append((method, cell_range, values))
            return {}

        sheets.request = request
        sale = {"id": 4, "amount": "9500", "quantity": "107.5877", "cycle_id": "ignored",
                "completed_at": "2026-09-22T20:30:45+00:00"}
        await sheets.send(sale)
        await sheets.send(sale)
        self.assertEqual(calls, [("PUT", "A5:C5", [[107.5877, "23.09.2026", "03:30:45"]])] * 2)


if __name__ == "__main__":
    unittest.main()
