import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from adspower import AdsPower


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "stop_adspower_profile.py"
SPEC = importlib.util.spec_from_file_location("stop_adspower_profile_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StopProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_only_configured_p1_profile(self):
        browser = AdsPower("http://127.0.0.1:50325", "test-key", "P1")
        requested = []

        async def handler(request):
            requested.append(request)
            return httpx.Response(200, json={"code": 0})

        real_client = httpx.AsyncClient
        with patch.object(MODULE.AdsPower, "from_env", return_value=browser), \
                patch.object(MODULE.httpx, "AsyncClient",
                             side_effect=lambda **_: real_client(transport=httpx.MockTransport(handler))), \
                patch.object(MODULE, "load_dotenv"):
            await MODULE.main()

        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0].url.path, "/api/v1/browser/stop")
        self.assertEqual(requested[0].url.params["user_id"], "P1")
        self.assertEqual(requested[0].headers["Authorization"], "Bearer test-key")
