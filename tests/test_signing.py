import hashlib
import hmac
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from mexc_client import MexcAPIError, MexcP2PClient


class SigningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MexcP2PClient("test-key", "test-secret")

    async def asyncTearDown(self):
        await self.client.close()

    def test_spaces_unicode_literal_plus_and_json_are_encoded_before_signing(self):
        params = {"tradeTerms": "А Б + & 💵", "overVerify": '{"types":[1,3]}',
                  "onlyTradeKybUser": False, "empty": "", "omit": None}
        expected = ("tradeTerms=%D0%90%20%D0%91%20%2B%20%26%20%F0%9F%92%B5"
                    "&overVerify=%7B%22types%22%3A%5B1%2C3%5D%7D&onlyTradeKybUser=false"
                    "&empty=&recvWindow=5000&timestamp=1000000")
        with patch("mexc_client.time.time", return_value=1000):
            query = self.client._signed_query(params)
        unsigned, signature = query.rsplit("&signature=", 1)
        self.assertEqual(unsigned, expected)
        self.assertEqual(signature, hmac.new(b"test-secret", expected.encode(), hashlib.sha256).hexdigest())

    async def test_post_transmits_exact_signed_query_without_body(self):
        async def handler(request):
            query = request.url.query.decode("ascii")
            unsigned, signature = query.rsplit("&signature=", 1)
            self.assertIn("tradeTerms=Cash%20%2B%20transfer", unsigned)
            self.assertNotIn("+", unsigned)
            self.assertEqual(signature, hmac.new(b"test-secret", unsigned.encode(), hashlib.sha256).hexdigest())
            self.assertEqual(request.content, b"")
            return httpx.Response(200, json={"code": 0, "data": "AD"})
        await self.client.close()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.client._request("POST", "/api/v3/fiat/merchant/ads/save_or_update",
                                   {"tradeTerms": "Cash + transfer"})

    async def test_signature_rejection_retains_code_without_retry(self):
        calls = []
        async def handler(request):
            calls.append(request)
            return httpx.Response(400, json={"code": 700002, "msg": "Invalid signature"})
        await self.client.close()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(MexcAPIError) as caught:
            await self.client._request("POST", "/api/v3/fiat/merchant/ads/save_or_update")
        self.assertEqual(caught.exception.code, 700002)
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(len(calls), 1)

    async def test_active_orders_includes_later_pages(self):
        first = [{'advOrderNo': str(i), 'createTime': i + 1} for i in range(50)]
        calls = []
        async def request(method, path, params):
            calls.append(dict(params))
            return {'data': first if len(calls) == 1 else [{'advOrderNo': 'outsider'}]}
        with patch.object(self.client, '_request', side_effect=request):
            orders = await self.client.list_active_maker_orders()
        self.assertEqual(len(orders), 51)
        self.assertEqual(orders[-1]['advOrderNo'], 'outsider')
        self.assertEqual(calls[1]['lastId'], '49')
        self.assertEqual(calls[1]['lastCreateTime'], 50)

    async def test_repeated_active_order_page_fails_closed(self):
        page = [{'advOrderNo': str(i), 'createTime': i + 1} for i in range(50)]
        with patch.object(self.client, '_request', new=AsyncMock(return_value={'data': page})):
            with self.assertRaises(MexcAPIError):
                await self.client.list_active_maker_orders()
