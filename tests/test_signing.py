import hashlib
import hmac
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from mexc_client import MexcAPIError, MexcChatUnavailable, MexcMutationUnknown, MexcP2PClient, MexcReadUnavailable


class SigningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MexcP2PClient("test-key", "test-secret")

    async def asyncTearDown(self):
        await self.client.close()

    async def test_read_and_connect_timeouts_are_distinct_from_unknown_post_result(self):
        for timeout_type in (httpx.ReadTimeout, httpx.ConnectTimeout):
            with self.subTest(timeout=timeout_type.__name__):
                async def timeout(request):
                    raise timeout_type('slow', request=request)
                await self.client.close()
                self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
                with self.assertRaises(MexcReadUnavailable):
                    await self.client._request('GET', '/api/v3/fiat/order/detail')
                with self.assertRaises(MexcMutationUnknown) as caught:
                    await self.client._request('POST', '/api/v3/fiat/release_coin')
                self.assertNotIsInstance(caught.exception, MexcReadUnavailable)

    async def test_chat_proxy_reset_is_classified_for_automatic_retry(self):
        await self.client.close()
        client = MexcP2PClient('test-key', 'test-secret', proxy_url='http://proxy.example:8080')
        try:
            client.generate_listen_key = AsyncMock(return_value='KEY')
            client.get_conversation_id = AsyncMock(return_value=12)
            with patch('mexc_client.Proxy.from_url') as proxy:
                proxy.return_value.connect = AsyncMock(side_effect=ConnectionResetError())
                with self.assertRaises(MexcChatUnavailable):
                    await client.send_chat_text('ORDER', 'Hello')
        finally:
            await client.close()

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

    async def test_chat_uses_same_explicit_proxy_as_http(self):
        await self.client.close()
        with patch('mexc_client.httpx.AsyncClient') as http_client:
            client = MexcP2PClient('test-key', 'test-secret', proxy_url='http://proxy.example:8080')
            self.assertEqual(http_client.call_args.kwargs['proxy'], 'http://proxy.example:8080')
            self.assertFalse(http_client.call_args.kwargs['trust_env'])
        client.generate_listen_key = AsyncMock(return_value='KEY')
        client.get_conversation_id = AsyncMock(return_value=12)
        with patch('mexc_client.Proxy.from_url') as proxy, patch('mexc_client.websockets.connect') as connect:
            sock = MagicMock()
            proxy.return_value.connect = AsyncMock(return_value=sock)
            ws = connect.return_value.__aenter__.return_value
            ws.recv = AsyncMock(return_value='{"success":true}')
            ws.send = AsyncMock()
            await client.send_chat_text('ORDER', 'Hello')
            proxy.assert_called_once_with('http://proxy.example:8080')
            proxy.return_value.connect.assert_awaited_once_with(
                dest_host='fiat.mexc.com', dest_port=443, timeout=15)
            self.assertEqual(connect.call_args.kwargs['proxy'], None)
            self.assertIs(connect.call_args.kwargs['sock'], sock)
            sock.close.assert_called_once()

    async def test_chat_without_profile_proxy_does_not_use_system_proxy(self):
        self.client.generate_listen_key = AsyncMock(return_value='KEY')
        self.client.get_conversation_id = AsyncMock(return_value=12)
        with patch('mexc_client.Proxy.from_url') as proxy, patch('mexc_client.websockets.connect') as connect:
            ws = connect.return_value.__aenter__.return_value
            ws.recv = AsyncMock(return_value='{"success":true}')
            ws.send = AsyncMock()
            await self.client.send_chat_text('ORDER', 'Hello')
            proxy.assert_not_called()
            self.assertIsNone(connect.call_args.kwargs['proxy'])
            self.assertNotIn('sock', connect.call_args.kwargs)

    async def test_wallet_transfer_is_only_between_otc_and_spot(self):
        requests = []
        async def handler(request):
            requests.append(request)
            if request.method == 'POST':
                return httpx.Response(200, json=[{'tranId': 'TEST-TRAN'}])
            return httpx.Response(200, json={
                'tranId': 'TEST-TRAN', 'asset': 'USDT', 'amount': '10',
                'fromAccountType': 'OTC', 'toAccountType': 'SPOT', 'status': 'SUCCESS'})
        await self.client.close()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertEqual(await self.client.transfer_usdt('OTC', 'SPOT', '10'), 'TEST-TRAN')
        self.assertEqual((await self.client.get_wallet_transfer('TEST-TRAN'))['status'], 'SUCCESS')
        self.assertEqual(requests[0].url.path, '/api/v3/capital/transfer')
        self.assertEqual(requests[0].url.params['asset'], 'USDT')
        self.assertEqual(requests[0].url.params['fromAccountType'], 'OTC')
        self.assertEqual(requests[0].url.params['toAccountType'], 'SPOT')
        self.assertEqual(requests[1].url.params['tranId'], 'TEST-TRAN')
        with self.assertRaises(ValueError):
            await self.client.transfer_usdt('FUTURES', 'SPOT', '10')

    async def test_withdraw_has_network_and_unique_request_id(self):
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'id': 'WITHDRAW-1'})
        await self.client.close()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertEqual(await self.client.withdraw_usdt('9.9', 'PLASMA', '0x' + '1' * 40,
                                                         '', 'UNIQUE-1'), 'WITHDRAW-1')
        query = requests[0].url.params
        self.assertEqual(requests[0].url.path, '/api/v3/capital/withdraw')
        self.assertEqual((query['coin'], query['netWork'], query['withdrawOrderId']),
                         ('USDT', 'PLASMA', 'UNIQUE-1'))
        self.assertNotIn('memo', query)

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

    async def test_timestamp_rejection_syncs_server_and_retries_once(self):
        calls = []
        async def handler(request):
            calls.append(request)
            if request.url.path == '/api/v3/time':
                return httpx.Response(200, json={'serverTime': 1010000})
            if len(calls) == 1:
                return httpx.Response(400, json={'code': 700003, 'msg': 'Timestamp outside recvWindow'})
            return httpx.Response(200, json={'code': 0, 'data': 'ORDER-1'})
        await self.client.close()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('mexc_client.time.time', return_value=1000):
            result = await self.client._request('POST', '/api/v3/fiat/merchant/order/deal', {'amount': '100'})
        self.assertEqual(result['data'], 'ORDER-1')
        self.assertEqual([r.url.path for r in calls],
            ['/api/v3/fiat/merchant/order/deal', '/api/v3/time', '/api/v3/fiat/merchant/order/deal'])
        self.assertEqual(calls[0].url.params['timestamp'], '1000000')
        self.assertEqual(calls[2].url.params['timestamp'], '1010000')

    async def test_timestamp_sync_failure_does_not_retry_mutation(self):
        calls = []
        async def handler(request):
            calls.append(request)
            if request.url.path == '/api/v3/time':
                return httpx.Response(503)
            return httpx.Response(400, json={'code': 700003, 'msg': 'Timestamp outside recvWindow'})
        await self.client.close()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(MexcAPIError) as caught:
            await self.client._request('POST', '/api/v3/fiat/merchant/order/deal')
        self.assertEqual(caught.exception.code, 700003)
        self.assertEqual(len(calls), 2)

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
