import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from deploy import manage_adspower_profile, open_adspower_mexc
from main import build_parser


class LinuxDeployTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_commands_only_change_requested_profile(self):
        calls = []

        async def handler(request):
            calls.append((request.url.path, request.url.params.get('user_id')))
            if request.url.path.endswith('/active'):
                status = 'Active' if len(calls) > 1 else 'Inactive'
                return httpx.Response(200, json={'code': 0, 'data': {'status': status}})
            return httpx.Response(200, json={'code': 0, 'data': {}})

        original_client = httpx.AsyncClient
        configured = SimpleNamespace(base_url='http://127.0.0.1:50325', api_key='test-key')
        with patch.object(manage_adspower_profile.AdsPower, 'from_env', return_value=configured), \
                patch('adspower.httpx.AsyncClient', side_effect=lambda **_: original_client(
                    transport=httpx.MockTransport(handler))), \
                patch('adspower.AdsPower.endpoint', new=AsyncMock()), \
                patch('adspower.asyncio.sleep', new=AsyncMock()):
            await manage_adspower_profile.manage_profile('browser-open', 'chosen123')
            await manage_adspower_profile.manage_profile('browser-close', 'chosen123')

        self.assertEqual(calls, [
            ('/api/v1/browser/active', 'chosen123'),
            ('/api/v1/browser/start', 'chosen123'),
            ('/api/v1/browser/active', 'chosen123'),
            ('/api/v1/browser/stop', 'chosen123'),
        ])
        self.assertEqual(build_parser().parse_args(['browser-open', 'chosen123']).profile_id,
                         'chosen123')
        self.assertEqual(build_parser().parse_args(['browser-close', 'chosen123']).profile_id,
                         'chosen123')

    async def test_browser_commands_reject_invalid_profile_id_before_api(self):
        with patch.object(manage_adspower_profile.AdsPower, 'from_env') as configured:
            with self.assertRaises(ValueError):
                await manage_adspower_profile.manage_profile('browser-close', '../other')
        configured.assert_not_called()

    def test_parallel_instances_load_only_the_selected_env_file(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / 'eflp.env'
            selected.write_text('TELEGRAM_BOT_TOKEN=selected-bot\nCYCLE_DB=data/eflp.sqlite3\n',
                                encoding='utf-8')
            env = os.environ.copy()
            env['MEXC_ENV_FILE'] = str(selected)
            env.pop('TELEGRAM_BOT_TOKEN', None)
            env.pop('CYCLE_DB', None)
            result = subprocess.run([sys.executable, '-c',
                'import config, os; print(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["CYCLE_DB"])'],
                cwd=root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'selected-bot data/eflp.sqlite3')

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
