import asyncio
import os
import unittest
from unittest.mock import patch

from config import Settings
from mexc_client import MexcAPIError, MexcP2PClient


class ProxyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_p2_mexc_request_reaches_configured_proxy(self):
        seen = []

        async def handle(reader, writer):
            seen.append((await reader.readline()).decode('ascii').strip())
            while await reader.readline() != b'\r\n':
                pass
            writer.write(b'HTTP/1.1 502 Test proxy observed CONNECT\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        env = {'MEXC_P2_2_API_KEY': 'test', 'MEXC_P2_2_SECRET_KEY': 'test',
               'MEXC_P2_2_PROXY_URL': f'http://user:pass@127.0.0.1:{port}'}
        try:
            with patch.dict(os.environ, env):
                settings = Settings.from_env('p2', p2_profile='2')
                client = MexcP2PClient(settings.api_key, settings.secret_key,
                                       proxy_url=settings.proxy_url)
                try:
                    with self.assertRaises(MexcAPIError):
                        await client._request('GET', '/api/v3/fiat/merchant/ads/pagination')
                finally:
                    await client.close()
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(seen, ['CONNECT api.mexc.com:443 HTTP/1.1'])
