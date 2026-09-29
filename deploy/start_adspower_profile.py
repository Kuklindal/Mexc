"""Start this instance's AdsPower P1 profile before its Telegram listener."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower  # noqa: E402


async def main() -> None:
    load_dotenv(ROOT / ".env")
    browser = AdsPower.from_env()
    if not browser.api_key or not browser.profile_id:
        raise RuntimeError("ADSPOWER_API_KEY и ADSPOWER_P1_PROFILE_ID обязательны")

    headers = {"Authorization": "Bearer " + browser.api_key}
    started = False
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for _ in range(24):
            try:
                response = await client.get(browser.base_url + "/api/v1/browser/active",
                    params={"user_id": browser.profile_id}, headers=headers)
            except httpx.RequestError:
                await asyncio.sleep(5)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"AdsPower Local API: HTTP {response.status_code}")
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError("AdsPower отклонил проверку профиля; проверьте API-ключ и ID")
            if payload.get("data", {}).get("status") == "Active":
                # The trading code also requires a local CDP endpoint.
                await browser.endpoint()
                print("AdsPower: профиль П1 активен", flush=True)
                return
            if not started:
                response = await client.get(browser.base_url + "/api/v1/browser/start",
                    params={"user_id": browser.profile_id}, headers=headers)
                if response.status_code != 200 or response.json().get("code") != 0:
                    raise RuntimeError("AdsPower не открыл профиль П1; проверьте его в Local API")
                started = True
            await asyncio.sleep(5)
    raise RuntimeError("AdsPower не подтвердил запуск профиля П1 за 120 секунд")


if __name__ == "__main__":
    asyncio.run(main())
