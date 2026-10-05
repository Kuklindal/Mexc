import unittest
import json
import httpx
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from adspower import (AdsPower, AdsPowerClickUnknown, AdsPowerError, AdsPowerTimeout,
                      AdsPowerUnavailable, local_url)


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
        self.browser.server_verification_state = AsyncMock(return_value="ready")

    async def test_local_api_read_timeout_retries_without_browser_command(self):
        response = httpx.Response(200, json={'code': 0, 'data': {'status': 'Active',
            'ws': {'puppeteer': 'ws://127.0.0.1:9222/devtools/browser/one'}}})
        with patch('adspower.httpx.AsyncClient') as factory, \
                patch('adspower.asyncio.sleep', new=AsyncMock()) as sleep:
            client = factory.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=[httpx.ReadTimeout('temporary'), response])
            endpoint = await self.browser.endpoint()
        self.assertEqual(endpoint, 'ws://127.0.0.1:9222/devtools/browser/one')
        self.assertEqual(client.get.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_persistent_local_api_timeout_is_classified_for_scheduler(self):
        with patch('adspower.httpx.AsyncClient') as factory, \
                patch('adspower.asyncio.sleep', new=AsyncMock()):
            client = factory.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=httpx.ReadTimeout('temporary'))
            with self.assertRaises(AdsPowerUnavailable):
                await self.browser.endpoint()
        self.assertEqual(client.get.await_count, 3)

    async def test_only_non_p1_local_profiles_are_closed(self):
        stopped = []

        async def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer secret-test-key')
            if request.url.path.endswith('/local-active'):
                return httpx.Response(200, json={'code': 0, 'data': {'list': [
                    {'user_id': 'P2'}, {'user_id': 'P1'}, {'user_id': 'P3'}]}})
            self.assertTrue(request.url.path.endswith('/stop'))
            stopped.append(request.url.params['user_id'])
            return httpx.Response(200, json={'code': 0, 'data': {}})

        original_client = httpx.AsyncClient
        with patch('adspower.httpx.AsyncClient',
                   side_effect=lambda **_: original_client(transport=httpx.MockTransport(handler))):
            self.assertEqual(await self.browser.close_other_local_profiles(), 2)
        self.assertEqual(stopped, ['P2', 'P3'])

    async def test_parallel_instances_do_not_close_each_others_profiles(self):
        with patch.dict('os.environ', {'ADSPOWER_CLOSE_OTHER_PROFILES': 'false'}), \
                patch('adspower.httpx.AsyncClient') as client:
            self.assertEqual(await self.browser.close_other_local_profiles(), 0)
        client.assert_not_called()

    async def test_active_maker_profile_is_not_closed(self):
        stopped = []
        async def handler(request):
            if request.url.path.endswith('/local-active'):
                return httpx.Response(200, json={'code': 0, 'data': {'list': [
                    {'user_id': 'P1'}, {'user_id': 'P2'}, {'user_id': 'P3'}]}})
            stopped.append(request.url.params['user_id'])
            return httpx.Response(200, json={'code': 0, 'data': {}})
        original_client = httpx.AsyncClient
        with patch('adspower.httpx.AsyncClient',
                   side_effect=lambda **_: original_client(transport=httpx.MockTransport(handler))):
            self.assertEqual(await self.browser.close_other_local_profiles({'P2'}), 1)
        self.assertEqual(stopped, ['P3'])

    async def test_dynamic_p1_is_protected_when_static_p1_is_not_open(self):
        stopped = []
        async def handler(request):
            if request.url.path.endswith('/local-active'):
                return httpx.Response(200, json={'code': 0, 'data': {'list': [
                    {'user_id': 'DYNAMIC-P1'}, {'user_id': 'MAKER-P2'}, {'user_id': 'OTHER'}]}})
            stopped.append(request.url.params['user_id'])
            return httpx.Response(200, json={'code': 0, 'data': {}})
        original_client = httpx.AsyncClient
        with patch('adspower.httpx.AsyncClient',
                   side_effect=lambda **_: original_client(transport=httpx.MockTransport(handler))):
            self.assertEqual(await self.browser.close_other_local_profiles(
                {'DYNAMIC-P1', 'MAKER-P2'}), 1)
        self.assertEqual(stopped, ['OTHER'])

    async def test_ensure_mexc_page_reuses_existing_tab(self):
        self.call.side_effect = [
            {'targetInfos': [{'type': 'page',
                'url': 'https://www.mexc.com/ru-RU/buy-crypto/', 'targetId': 'mexc'}]},
            {'sessionId': 'session'},
            {'result': {'value': {'ready': 'complete', 'bodyChars': 100}}},
        ]
        await self.browser.ensure_mexc_page()
        self.assertEqual([call.args[0] for call in self.call.await_args_list],
                         ['Target.getTargets', 'Target.attachToTarget', 'Runtime.evaluate'])

    async def test_ensure_mexc_page_creates_one_tab(self):
        self.call.side_effect = [
            {'targetInfos': []}, {'targetId': 'new'},
            {'targetInfos': [{'type': 'page', 'url': 'https://www.mexc.com/ru-RU/buy-crypto/',
                               'targetId': 'new'}]},
            {'sessionId': 'session'},
            {'result': {'value': {'ready': 'interactive', 'bodyChars': 25}}},
        ]
        await self.browser.ensure_mexc_page()
        self.assertEqual(self.call.await_count, 5)
        self.assertEqual(self.call.call_args_list[1].args[0], 'Target.createTarget')

    async def test_ensure_mexc_page_rejects_http_success_without_rendered_body(self):
        async def call(method, *args, **kwargs):
            if method == 'Target.getTargets':
                return {'targetInfos': [{'type': 'page',
                    'url': 'https://www.mexc.com/ru-RU/buy-crypto/', 'targetId': 'mexc'}]}
            if method == 'Target.attachToTarget':
                return {'sessionId': 'session'}
            if method == 'Runtime.evaluate':
                return {'result': {'value': {'ready': 'loading', 'bodyChars': 0}}}
            self.fail(f'Unexpected browser call: {method}')
        self.call.side_effect = call
        with patch('adspower.asyncio.sleep', new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(AdsPowerUnavailable, 'не загрузила содержимое'):
                await self.browser.ensure_mexc_page()
        self.assertEqual(sleep.await_count, 15)

    async def test_other_profiles_stay_open_if_p1_is_missing(self):
        async def handler(request):
            self.assertTrue(request.url.path.endswith('/local-active'))
            return httpx.Response(200, json={'code': 0, 'data': {'list': [{'user_id': 'P2'}]}})

        original_client = httpx.AsyncClient
        with patch('adspower.httpx.AsyncClient',
                   side_effect=lambda **_: original_client(transport=httpx.MockTransport(handler))):
            with self.assertRaisesRegex(AdsPowerError, 'П1 не открыт'):
                await self.browser.close_other_local_profiles()

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
        self.browser.verification_state.side_effect = AdsPowerError('Сервер MEXC не подтвердил состояние')
        with patch("adspower.asyncio.sleep", new=AsyncMock()), self.assertRaises(AdsPowerError):
            await self.browser.open_order(ORDER)
        self.assertFalse(any(c.args[0] == "Target.createTarget" for c in self.call.call_args_list))

    async def test_missing_standalone_button_checks_exact_server_order_without_click(self):
        target = {"type": "page", "targetId": "existing",
                  "url": "https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=" + ORDER}
        for server_state in ('passed', 'not_required'):
            self.call.reset_mock(side_effect=True)
            self.call.side_effect = [{"targetInfos": [target]}, {"sessionId": "s"}]
            self.browser.locate = AsyncMock(return_value=None)
            self.browser.view = AsyncMock(return_value='missing')
            self.browser.verification_state = AsyncMock(return_value=server_state)
            with patch('adspower.asyncio.sleep', new=AsyncMock()):
                self.assertEqual(await self.browser.open_order(ORDER), server_state)
            self.browser.verification_state.assert_awaited_once_with(self.call, 's', ORDER)
            self.assertTrue(all(not call.kwargs.get('click') for call in self.browser.view.call_args_list))

    async def test_missing_standalone_button_opens_exact_merchant_order_if_required(self):
        target = {"type": "page", "targetId": "existing",
                  "url": "https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=" + ORDER}
        self.call.side_effect = [{"targetInfos": [target]}, {"sessionId": "s"}]
        self.browser.locate = AsyncMock(return_value=None)
        self.browser.view = AsyncMock(return_value='missing')
        self.browser.verification_state.return_value = 'ready'
        self.browser.open_merchant_order = AsyncMock(return_value='ready')
        with patch('adspower.asyncio.sleep', new=AsyncMock()):
            self.assertEqual(await self.browser.open_order(ORDER), 'ready')
        self.browser.open_merchant_order.assert_awaited_once_with(self.call, ORDER)

    async def test_merchant_portal_is_opened_only_for_exact_order_row(self):
        self.call.side_effect = [
            {'targetInfos': []}, {'targetId': 'control'}, {'sessionId': 's'},
            {'result': {'value': False}}, {'result': {'value': True}},
        ]
        self.browser.read_view = AsyncMock(return_value='ready')
        with patch('adspower.asyncio.sleep', new=AsyncMock()):
            self.assertEqual(await self.browser.open_merchant_order(self.call, ORDER), 'ready')
        self.assertEqual(self.call.call_args_list[1].args[0], 'Target.createTarget')
        self.assertIn('/buy-crypto/control', self.call.call_args_list[1].args[1]['url'])
        self.assertEqual(self.call.call_args_list[4].args[2], 's')
        self.assertIn(ORDER, self.call.call_args_list[4].args[1]['expression'])

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

    async def test_lost_click_response_is_reconciled_without_second_click(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock(side_effect=AdsPowerClickUnknown("reply lost"))
        self.browser.server_verification_state = AsyncMock(return_value="passed")
        await self.browser.approve(ORDER)
        self.browser.view.assert_awaited_once()
        self.browser.server_verification_state.assert_awaited_once_with(ORDER)

    async def test_post_click_read_timeout_is_reconciled_without_second_click(self):
        self.browser.locate = AsyncMock(return_value=("session", "ready"))
        self.browser.view = AsyncMock(side_effect=["clicked", AdsPowerTimeout("read timeout")])
        self.browser.server_verification_state = AsyncMock(return_value="passed")
        with patch("adspower.asyncio.sleep", new=AsyncMock()):
            await self.browser.approve(ORDER)
        self.assertEqual(self.browser.view.await_count, 2)
        self.assertEqual(sum(c.kwargs.get("click", False) for c in self.browser.view.call_args_list), 1)
        self.browser.server_verification_state.assert_awaited_once_with(ORDER)

    async def test_server_verification_uses_another_p1_tab_if_one_is_stalled(self):
        pages = [
            {'type': 'page', 'targetId': 'one',
             'url': 'https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=' + ORDER},
            {'type': 'page', 'targetId': 'two',
             'url': 'https://www.mexc.com/ru-RU/buy-crypto/control'},
        ]
        self.call.side_effect = [{'targetInfos': pages}, {'sessionId': 's1'}, {'sessionId': 's2'}]
        self.browser.verification_state.side_effect = [AdsPowerTimeout('stalled'), 'passed']
        self.assertEqual(await AdsPower.server_verification_state(self.browser, ORDER), 'passed')
        self.assertEqual(self.browser.verification_state.await_count, 2)

    async def test_click_uses_longer_cdp_timeout_than_read(self):
        self.call.return_value = {"result": {"value": {"state": "clicked"}}}
        self.assertEqual(await AdsPower.view(self.browser, self.call, "s", ORDER, click=True), "clicked")
        self.assertEqual(self.call.call_args.kwargs["timeout"], 30)
        self.call.reset_mock()
        self.call.return_value = {"result": {"value": {"state": "ready"}}}
        self.assertEqual(await AdsPower.view(self.browser, self.call, "s", ORDER), "ready")
        self.assertNotIn("timeout", self.call.call_args.kwargs)

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
        with patch("adspower.websockets.connect", new=connection), \
                patch("adspower.asyncio.timeout", None, create=True):
            async with browser.connection() as call:
                with self.assertRaises(AdsPowerTimeout):
                    await call("Runtime.evaluate", session="s")
                self.assertEqual(await call("Target.activateTarget", {"targetId": "tab"}), {"activated": True})

    async def test_malformed_cdp_result_reports_command_instead_of_attribute_error(self):
        browser = AdsPower("http://127.0.0.1:53152", "test-key", "P1")
        browser.endpoint = AsyncMock(return_value="ws://127.0.0.1:1234/debug")
        ws = AsyncMock()
        ws.recv.return_value = json.dumps({"id": 1, "result": None})
        @asynccontextmanager
        async def connection(*args, **kwargs):
            yield ws
        with patch("adspower.websockets.connect", new=connection):
            with self.assertRaisesRegex(AdsPowerError, "некорректный результат CDP для Target.getTargets"):
                async with browser.connection() as call:
                    await call("Target.getTargets")

    async def test_unexpected_error_reports_safe_code_location(self):
        browser = AdsPower("http://127.0.0.1:53152", "test-key", "P1")
        browser.endpoint = AsyncMock(return_value="ws://127.0.0.1:1234/debug")
        ws = AsyncMock()
        ws.recv.return_value = json.dumps({"id": 1, "result": {}})
        @asynccontextmanager
        async def connection(*args, **kwargs):
            yield ws
        with patch("adspower.websockets.connect", new=connection):
            with self.assertRaisesRegex(AdsPowerError, r"AttributeError, test_adspower.py:\d+") as caught:
                async with browser.connection() as call:
                    await call("Target.getTargets")
                    raise AttributeError("secret-token")
        self.assertNotIn("secret-token", str(caught.exception))

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
