import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from deploy import open_adspower_mexc
from main import build_parser


class LinuxDeployTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_mexc_command_requires_rendered_page(self):
        browser = SimpleNamespace(ensure_mexc_page=AsyncMock())
        with patch.object(open_adspower_mexc, 'load_dotenv'), \
                patch.object(open_adspower_mexc, 'start_profile', new=AsyncMock()), \
                patch.object(open_adspower_mexc.AdsPower, 'from_env', return_value=browser):
            await open_adspower_mexc.main()
        self.assertEqual(build_parser().parse_args(['adspower-open']).command, 'adspower-open')
        browser.ensure_mexc_page.assert_awaited_once()

    async def test_open_mexc_command_does_not_report_loading_tab_as_success(self):
        from adspower import AdsPowerUnavailable
        browser = SimpleNamespace(ensure_mexc_page=AsyncMock(
            side_effect=AdsPowerUnavailable('Страница ещё загружается')))
        with patch.object(open_adspower_mexc, 'load_dotenv'), \
                patch.object(open_adspower_mexc, 'start_profile', new=AsyncMock()) as start, \
                patch.object(open_adspower_mexc.AdsPower, 'from_env', return_value=browser):
            with self.assertRaises(AdsPowerUnavailable):
                await open_adspower_mexc.main()
        start.assert_awaited_once()

    async def test_profile_bootstrap_reports_start_error_without_api_key(self):
        path = Path(__file__).resolve().parents[1] / 'deploy' / 'start_adspower_profile.py'
        spec = importlib.util.spec_from_file_location('start_adspower_profile_error_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        browser = SimpleNamespace(base_url='http://127.0.0.1:50325', api_key='test-key',
                                  profile_id='P1')

        async def handler(request):
            if request.url.path.endswith('/start'):
                return httpx.Response(200, json={'code': 100001, 'msg': 'Failed test-key'})
            return httpx.Response(200, json={'code': 0, 'data': {'status': 'Inactive'}})

        original_client = httpx.AsyncClient
        with patch.object(module, 'load_dotenv'), \
                patch.object(module.AdsPower, 'from_env', return_value=browser), \
                patch.object(module.httpx, 'AsyncClient',
                             side_effect=lambda **_: original_client(transport=httpx.MockTransport(handler))):
            with self.assertRaisesRegex(RuntimeError, 'код 100001') as raised:
                await module.main()
        self.assertIn('Failed [скрыто]', str(raised.exception))
        self.assertNotIn('test-key', str(raised.exception))

    async def test_profile_bootstrap_starts_only_inactive_profile(self):
        path = Path(__file__).resolve().parents[1] / 'deploy' / 'start_adspower_profile.py'
        spec = importlib.util.spec_from_file_location('start_adspower_profile_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        browser = SimpleNamespace(base_url='http://127.0.0.1:50325', api_key='test-key',
                                  profile_id='P1', endpoint=AsyncMock(return_value='ws://127.0.0.1:1'),
                                  close_other_local_profiles=AsyncMock(return_value=2))
        starts = 0

        async def handler(request):
            nonlocal starts
            self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
            self.assertEqual(request.url.params['user_id'], 'P1')
            if request.url.path.endswith('/start'):
                self.assertEqual(request.url.params['headless'], '1')
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
        browser.close_other_local_profiles.assert_awaited_once()
