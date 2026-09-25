import unittest
from unittest.mock import AsyncMock

from mexc_client import MexcAPIError, MexcP2PClient


class AdLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MexcP2PClient('test-key', 'test-secret')
        self.client._request = AsyncMock()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_lookup_includes_all_supported_ad_statuses(self):
        for status in ('OPEN', 'CLOSE', 'LOW_STOCK'):
            ad = {'advNo': 'AD-SELL', 'advStatus': status}
            self.client._request.return_value = {'code': 0, 'data': [ad]}
            self.assertEqual(await self.client.get_ad('AD-SELL'), ad)
            self.client._request.assert_awaited_with('GET', '/api/v3/fiat/merchant/ads/pagination',
                {'advNo': 'AD-SELL', 'advStatus': 'OPEN,CLOSE,LOW_STOCK', 'page': 1, 'limit': 10})

    async def test_empty_page_without_data_is_not_a_format_error(self):
        self.client._request.return_value = {'code': 0, 'msg': 'success', 'page': {'total': 0}}
        with self.assertRaisesRegex(MexcAPIError, 'AD-SELL.*не найдено'):
            await self.client.get_ad('AD-SELL')

    async def test_malformed_or_ambiguous_response_never_selects_another_ad(self):
        for payload in ({'data': {}}, {'page': {'total': 1}}, {'data': [{'advNo': 'OTHER'}]},
                        {'data': [{'advNo': 'AD-SELL'}, {'advNo': 'AD-SELL'}]}):
            self.client._request.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(MexcAPIError):
                await self.client.get_ad('AD-SELL')
