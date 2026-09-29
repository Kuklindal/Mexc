import json
import unittest
from urllib.parse import parse_qs
from unittest.mock import AsyncMock
from decimal import Decimal

import httpx

from cycle import Paused
from mexc_client import MexcP2PClient, ad_replenish_params, ad_verification
import test_cycle as fixtures


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CycleTests.setUp
    tearDown = fixtures.CycleTests.tearDown
    runner = fixtures.CycleTests.runner

    def test_missing_field_uses_config_and_serializes_as_json(self):
        ad = dict(self.exchange.ad)
        del ad['overVerify']
        value = ad_verification(ad, '{"types":[1]}')
        params = ad_replenish_params(ad | {'overVerify': value}, '100')
        self.assertEqual(params['overVerify'], '{"types":[1]}')

    def test_remote_setting_preserved_instead_of_fallback(self):
        for value in ({'types': [3, 1]}, '{"types":[3,1]}'):
            self.assertEqual(ad_verification({'overVerify': value}, '{"types":[1]}'), '{"types":[1,3]}')
        value = {'types': [6], 'otherText': 'Справка + фото'}
        self.assertEqual(json.loads(ad_verification({'overVerify': value})), value)

    def test_missing_or_malformed_settings_never_silently_omitted(self):
        for value in (None, '', 'bad json', 'null', {}, {'types': []}, {'types': [True]},
                      {'types': [1, 1]}, {'types': [7]}, {'types': [1, 2, 3, 4]}, {'types': [6]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ad_replenish_params(self.exchange.ad | {'overVerify': value}, '100')

    async def test_cycle_with_omitted_api_field_uses_quantity_only_without_resaving_ad(self):
        del self.exchange.ad['overVerify']
        self.exchange.ad['maxSingleTransAmount'] = '50000'
        runner = self.runner()
        runner.p1_over_verify = '{"types":[1]}'
        browser = type('Browser', (), {})()
        browser.open_order = AsyncMock(return_value='ready')
        browser.inspect = AsyncMock(return_value='passed')
        browser.approve = AsyncMock()
        browser.ad_details = AsyncMock(side_effect=lambda _: {'id': 'AD-SELL', 'coinName': 'USDT', 'currency': 'RUB',
            'tradeType': 1, 'availableQuantity': self.exchange.ad['availableQuantity'], 'overVerify': {'types': [1]}})
        async def replenish(plan):
            self.exchange.ad['availableQuantity'] = str(Decimal(self.exchange.ad['availableQuantity']) + Decimal(plan['quantity']))
        browser.replenish_ad = AsyncMock(side_effect=replenish)
        runner.browser = browser
        await runner.run(self.cycle_id)
        self.assertEqual(self.exchange.ad_calls, [])
        self.assertEqual(self.exchange.ad['availableQuantity'], '109')
        self.assertEqual(self.exchange.ad['maxSingleTransAmount'], '50000')
        browser.replenish_ad.assert_awaited_once()
        saved = self.journal.step(self.cycle_id, 'reverse_replenish')['result']
        self.assertEqual(saved['method'], 'quantity_only')
        self.assertEqual(saved['over_verify'], '{"types":[1]}')


    def test_changed_verification_invalidates_plan_and_postcheck(self):
        runner = self.runner()
        runner.spec = self.spec
        original = runner.replenish_plan(self.exchange.ad, 'AD-SELL', '100')
        changed = self.exchange.ad | {'overVerify': {'types': [2]}}
        self.assertNotEqual(original, runner.replenish_plan(changed, 'AD-SELL', '100'))
        with self.assertRaises(Paused):
            runner.check_replenished(changed | {'availableQuantity': '109'}, original)

    async def test_temporary_other_order_lock_does_not_invalidate_quantity_refill(self):
        runner = self.runner()
        runner.spec = self.spec
        ad = self.exchange.ad | {'availableQuantity': '9', 'frozenQuantity': '0'}
        actual = {'id': 'AD-SELL', 'coinName': 'USDT', 'currency': 'RUB',
                  'tradeType': 1, 'availableQuantity': '7', 'frozenQuantity': '2',
                  'overVerify': {'types': [1]}}
        browser = type('Browser', (), {})()
        browser.ad_details = AsyncMock(side_effect=lambda _: actual.copy())
        runner.browser = browser
        plan = await runner.quantity_plan(ad, 'AD-SELL', '100')
        self.assertEqual(plan['target_total'], '109')
        self.assertEqual(plan['target_available'], '107')
        actual.update(availableQuantity='6', frozenQuantity='103')
        after = ad | {'availableQuantity': '6', 'frozenQuantity': '103'}
        runner.check_replenished(after, plan)
        await runner.check_browser_ad(plan)
        after['frozenQuantity'] = '102'
        with self.assertRaises(Paused):
            runner.check_replenished(after, plan)

    async def test_actual_request_contains_url_encoded_json(self):
        client = MexcP2PClient('test-key', 'test-secret')
        await client.close()
        async def handler(request):
            query = request.url.query.decode('ascii')
            self.assertIn('overVerify=%7B%22types%22%3A%5B1%5D%7D', query)
            self.assertEqual(json.loads(parse_qs(query)['overVerify'][0]), {'types': [1]})
            return httpx.Response(200, json={'code': 0, 'data': 'AD-SELL'})
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.replenish_ad(self.exchange.ad, '100')
        finally:
            await client.close()
