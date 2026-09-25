import unittest
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from adspower import AdsPower, AdsPowerError, AdsPowerTimeout, local_url


ORDER = "d1823100470541552640"


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = AdsPower("http://127.0.0.1:53152", "secret-test-key", "P1")
        self.call = AsyncMock()
        @asynccontextmanager
        async def connection():
            yield self.call
        self.browser.connection = connection
        self.browser.verification_state = AsyncMock(return_value="passed")

    async def test_inspect_does_not_click(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock()
        self.assertEqual(await self.browser.inspect(ORDER), "ready")
        self.browser.view.assert_not_called()

    async def test_close_only_exact_completed_order_tabs(self):
        base = 'https://www.mexc.com/ru-RU/buy-crypto/'
        urls = [base + 'order-processing?id=' + ORDER,
                base + 'order-processing?id=d1111111111111111111',
                base + 'control', base + 'create-advertising/list',
                base + 'order-processing?id=' + ORDER + '&id=',
                'https://evil.example/ru-RU/buy-crypto/order-processing?id=' + ORDER]
        targets = [dict(type='page', targetId=str(i), url=url) for i, url in enumerate(urls)]
        self.call.side_effect = [{'targetInfos': targets}, {'targetInfo': targets[0]}, {'success': True}]
        self.assertEqual(await self.browser.close_order_tabs([ORDER]), 1)
        self.assertEqual(self.call.call_args_list[-1].args, ('Target.closeTarget', {'targetId': '0'}))
        self.assertEqual(self.call.await_count, 3)

    async def test_tab_navigated_after_listing_is_not_closed(self):
        target = dict(type='page', targetId='tab', url='https://mexc.com/ru-RU/buy-crypto/order-processing?id=' + ORDER)
        self.call.side_effect = [{'targetInfos': [target]}, {'targetInfo': dict(target, url='https://mexc.com/ru-RU/buy-crypto/control')}]
        self.assertEqual(await self.browser.close_order_tabs([ORDER]), 0)
        self.assertEqual(self.call.await_count, 2)

    async def test_open_existing_order_does_not_create_tab_or_click(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.assertEqual(await self.browser.open_order(ORDER), "ready")
        self.call.assert_not_called()

    async def test_open_order_creates_exact_url_without_approval(self):
        self.browser.locate = AsyncMock(return_value=None)
        self.call.side_effect = [{"targetInfos": []}, {"targetId": "new"}, {"sessionId": "session"}]
        self.browser.view = AsyncMock(side_effect=["missing", "ready"])
        with patch("adspower.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await self.browser.open_order(ORDER), "ready")
        self.assertEqual(self.call.call_args_list[1].args, ("Target.createTarget", {
            "url": "https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=" + ORDER, "background": False}))
        self.assertTrue(all(not c.kwargs.get("click") for c in self.browser.view.call_args_list))

    async def test_unloaded_existing_page_does_not_create_duplicate(self):
        self.browser.locate = AsyncMock(return_value=None)
        self.call.side_effect = [{"targetInfos": [{"type": "page", "targetId": "existing",
            "url": "https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=" + ORDER}]}, {"sessionId": "s"}]
        self.browser.view = AsyncMock(return_value="missing")
        with patch("adspower.asyncio.sleep", new=AsyncMock()), self.assertRaises(AdsPowerError):
            await self.browser.open_order(ORDER)
        self.assertFalse(any(c.args[0] == "Target.createTarget" for c in self.call.call_args_list))

    async def test_invalid_order_cannot_open_page(self):
        with self.assertRaises(AdsPowerError):
            await self.browser.open_order("not-an-order&url=evil")
        self.call.assert_not_called()

    async def test_already_passed_is_not_clicked_again(self):
        self.browser.locate = AsyncMock(return_value=("session", "passed"))
        self.browser.view = AsyncMock()
        await self.browser.approve(ORDER)
        self.browser.view.assert_not_called()

    async def test_false_payment_heading_is_not_completed_verification(self):
        self.browser.locate = AsyncMock(return_value=("session", "passed"))
        self.browser.verification_state.return_value = "ready"
        self.browser.view = AsyncMock()
        self.assertEqual(await self.browser.inspect(ORDER), "ready")
        with self.assertRaisesRegex(AdsPowerError, "ещё не пройдена"):
            await self.browser.approve(ORDER)
        self.browser.view.assert_not_called()

    async def test_false_payment_heading_opens_merchant_order_without_approval(self):
        self.browser.locate = AsyncMock(return_value=("session", "passed"))
        self.browser.verification_state.return_value = "ready"
        self.browser.open_merchant_order = AsyncMock(return_value="ready")
        self.assertEqual(await self.browser.open_order(ORDER), "ready")
        self.browser.open_merchant_order.assert_awaited_once_with(self.call, ORDER)

    async def test_click_heading_transition_without_server_confirmation_fails(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock(side_effect=["clicked"] + ["passed"] * 15)
        self.browser.verification_state.return_value = "ready"
        with patch("adspower.asyncio.sleep", new=AsyncMock()), self.assertRaises(AdsPowerError):
            await self.browser.approve(ORDER)
        self.assertEqual(sum(c.kwargs.get("click", False) for c in self.browser.view.call_args_list), 1)

    async def test_verification_response_requires_explicit_integer_state(self):
        for value, expected in (({'state': 0, 'required': True}, "ready"),
                                ({'state': 0, 'required': False}, 'not_required'),
                                ({'state': 0, 'required': None}, 'unknown'),
                                ({'state': 1, 'required': None}, "passed"),
                                (None, None), (True, None), ({'state': 2}, None), ({'state': "1"}, None)):
            self.call.return_value = {"result": {"value": value}}
            if expected:
                self.assertEqual(await AdsPower.verification_state(self.browser, self.call, "s", ORDER), expected)
            else:
                with self.assertRaises(AdsPowerError):
                    await AdsPower.verification_state(self.browser, self.call, "s", ORDER)

    async def test_click_requires_positive_result(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock(side_effect=["clicked", "blocked", "passed"])
        with patch("adspower.asyncio.sleep", new=AsyncMock()):
            await self.browser.approve(ORDER)
        self.assertEqual(sum(c.kwargs.get("click", False) for c in self.browser.view.call_args_list), 1)

    async def test_changed_order_or_button_blocks_click(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock(return_value="missing")
        with self.assertRaises(AdsPowerError):
            await self.browser.approve(ORDER)
        self.assertEqual(self.browser.view.await_count, 1)

    async def test_timeout_does_not_repeat_click(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock(side_effect=["clicked"] + ["ready"] * 15)
        with patch("adspower.asyncio.sleep", new=AsyncMock()), self.assertRaises(AdsPowerError):
            await self.browser.approve(ORDER)
        self.assertEqual(sum(c.kwargs.get("click", False) for c in self.browser.view.call_args_list), 1)

    async def test_unresponsive_tab_is_activated_and_only_read_is_retried(self):
        self.browser.view = AsyncMock(side_effect=[AdsPowerTimeout("timeout"), "ready"])
        self.assertEqual(await self.browser.read_view(self.call, "s", "tab", ORDER), "ready")
        self.call.assert_awaited_once_with("Target.activateTarget", {"targetId": "tab"})
        self.assertEqual(self.browser.view.await_count, 2)
        self.assertTrue(all(not c.kwargs.get("click") for c in self.browser.view.call_args_list))

    async def test_unresponsive_tab_stops_after_single_read_retry(self):
        self.browser.view = AsyncMock(side_effect=AdsPowerTimeout("timeout"))
        with self.assertRaisesRegex(AdsPowerError, "Кнопка проверки не нажималась"):
            await self.browser.read_view(self.call, "s", "tab", ORDER)
        self.assertEqual(self.browser.view.await_count, 2)
        self.call.assert_awaited_once()

    async def test_click_command_timeout_is_unknown_without_retry(self):
        self.browser.locate = AsyncMock(return_value=("s", "ready"))
        self.call.side_effect = AdsPowerTimeout("Runtime.evaluate")
        with self.assertRaisesRegex(AdsPowerError, "Результат неизвестен"):
            await self.browser.approve(ORDER)
        self.call.assert_awaited_once()

    async def test_no_tab_activation_when_read_responds(self):
        self.browser.view = AsyncMock(return_value="ready")
        self.assertEqual(await self.browser.read_view(self.call, "s", "tab", ORDER), "ready")
        self.call.assert_not_called()

    async def test_transport_reports_timed_out_command_without_payload(self):
        browser = AdsPower("http://127.0.0.1:53152", "secret-test-key", "P1")
        browser.endpoint = AsyncMock(return_value="ws://127.0.0.1:1234/debug")
        ws = AsyncMock()
        ws.recv.side_effect = TimeoutError()
        @asynccontextmanager
        async def connection(*args, **kwargs):
            yield ws
        with patch("adspower.websockets.connect", new=connection):
            with self.assertRaises(AdsPowerTimeout) as caught:
                async with browser.connection() as call:
                    await call("Runtime.evaluate", {"expression": "private-page-content"}, "s")
        self.assertIn("Runtime.evaluate", str(caught.exception))
        self.assertNotIn("private-page-content", str(caught.exception))
        ws.send.assert_awaited_once()

    async def test_late_response_after_timeout_does_not_satisfy_next_command(self):
        browser = AdsPower("http://127.0.0.1:53152", "test-key", "P1")
        browser.endpoint = AsyncMock(return_value="ws://127.0.0.1:1234/debug")
        ws = AsyncMock()
        ws.recv.side_effect = [TimeoutError(), json.dumps({"id": 1, "result": {"stale": True}}),
                               json.dumps({"id": 2, "result": {"activated": True}})]
        @asynccontextmanager
        async def connection(*args, **kwargs):
            yield ws
        with patch("adspower.websockets.connect", new=connection):
            async with browser.connection() as call:
                with self.assertRaises(AdsPowerTimeout):
                    await call("Runtime.evaluate", session="s")
                self.assertEqual(await call("Target.activateTarget", {"targetId": "tab"}), {"activated": True})

    async def test_duplicate_order_windows_block_approval(self):
        self.call.side_effect = [
            {"targetInfos": [{"targetId": x, "type": "page", "url": "https://www.mexc.com/ru-RU/buy-crypto/control"} for x in ("A", "B")]},
            {"sessionId": "A"}, {"sessionId": "B"}]
        self.browser.view = AsyncMock(return_value="ready")
        with self.assertRaises(AdsPowerError):
            await self.browser.approve(ORDER)
        self.assertTrue(all(not c.kwargs.get("click") for c in self.browser.view.call_args_list))

    async def test_other_domains_are_not_inspected(self):
        self.call.return_value = {"targetInfos": [{"targetId": "X", "type": "page", "url": "https://mexc.com.evil.test/ru-RU/buy-crypto/control"}]}
        self.browser.view = AsyncMock()
        with self.assertRaises(AdsPowerError):
            await self.browser.inspect(ORDER)
        self.browser.view.assert_not_called()

    def test_only_loopback_endpoints_allowed(self):
        for url in ("https://example.com", "http://127.0.0.1.evil.test", "http://user:password@localhost"):
            with self.subTest(url=url), self.assertRaises(AdsPowerError):
                local_url(url, {"http", "https"})
