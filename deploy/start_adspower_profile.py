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
from config import ENV_FILE  # noqa: E402


def api_error(response: httpx.Response, api_key: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}; ответ не является JSON"
    code = payload.get("code")
    message = str(payload.get("msg") or "").replace(api_key, "[скрыто]")[:300]
    return f"HTTP {response.status_code}; код {code}; {message}".rstrip("; ")


async def main() -> None:
    load_dotenv(ENV_FILE)
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
                raise RuntimeError("AdsPower Local API: " + api_error(response, browser.api_key))
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError("AdsPower отклонил проверку профиля: "
                                   + api_error(response, browser.api_key))
            if payload.get("data", {}).get("status") == "Active":
                # The trading code also requires a local CDP endpoint.
                await browser.endpoint()
                closed = await browser.close_other_local_profiles()
                if closed:
                    print(f"AdsPower: закрыто лишних профилей: {closed}", flush=True)
                print("AdsPower: профиль П1 активен", flush=True)
                return
            if not started:
                # The headless Local API does not make browser profiles headless.
                response = await client.get(browser.base_url + "/api/v1/browser/start",
                    params={"user_id": browser.profile_id, "headless": 1}, headers=headers)
                if response.status_code != 200 or response.json().get("code") != 0:
                    raise RuntimeError("AdsPower не открыл профиль П1: "
                                       + api_error(response, browser.api_key))
                started = True
            await asyncio.sleep(5)
    raise RuntimeError("AdsPower не подтвердил запуск профиля П1 за 120 секунд")


if __name__ == "__main__":
    asyncio.run(main())
