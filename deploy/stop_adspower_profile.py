"""Close this instance's AdsPower P1 browser profile."""
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


async def main() -> None:
    load_dotenv(ENV_FILE)
    browser = AdsPower.from_env()
    if not browser.api_key or not browser.profile_id:
        raise RuntimeError("ADSPOWER_API_KEY и ADSPOWER_P1_PROFILE_ID обязательны")

    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                browser.base_url + "/api/v1/browser/stop",
                params={"user_id": browser.profile_id},
                headers={"Authorization": "Bearer " + browser.api_key},
            )
            payload = response.json()
    except (httpx.RequestError, ValueError) as exc:
        raise RuntimeError(f"Не удалось закрыть профиль П1 ({type(exc).__name__})") from None

    if response.status_code != 200 or not isinstance(payload, dict) or payload.get("code") != 0:
        raise RuntimeError(f"AdsPower не подтвердил закрытие профиля П1 (HTTP {response.status_code})")
    print("AdsPower: профиль П1 закрыт", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
