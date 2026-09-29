import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx


class LinuxDeployTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_bootstrap_starts_only_inactive_profile(self):
        path = Path(__file__).resolve().parents[1] / 'deploy' / 'start_adspower_profile.py'
        spec = importlib.util.spec_from_file_location('start_adspower_profile_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        browser = SimpleNamespace(base_url='http://127.0.0.1:50325', api_key='test-key',
                                  profile_id='P1', endpoint=AsyncMock(return_value='ws://127.0.0.1:1'))
        starts = 0

        async def handler(request):
            nonlocal starts
            self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
            self.assertEqual(request.url.params['user_id'], 'P1')
            if request.url.path.endswith('/start'):
                starts += 1
                return httpx.Response(200, json={'code': 0})
            return httpx.Response(200, json={'code': 0,
                                             'data': {'status': 'Active' if starts else 'Inactive'}})

        original_client = httpx.AsyncClient
        with patch.object(module, 'load_dotenv'), \
                patch.object(module.AdsPower, 'from_env', return_value=browser), \
                patch.object(module.httpx, 'AsyncClient',
                             side_effect=lambda **_: original_client(transport=httpx.MockTransport(handler))), \
                patch.object(module.asyncio, 'sleep', new=AsyncMock()):
            await module.main()
        self.assertEqual(starts, 1)
        browser.endpoint.assert_awaited_once()
