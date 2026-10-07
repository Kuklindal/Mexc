"""Open or close exactly one AdsPower browser profile by its user ID."""
from __future__ import annotations

import re

from adspower import AdsPower


async def manage_profile(command: str, profile_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile_id):
        raise ValueError("ID профиля AdsPower: 1–64 латинских буквы, цифры, _ или -")

    configured = AdsPower.from_env()
    if not configured.api_key:
        raise RuntimeError("В выбранном .env не задан ADSPOWER_API_KEY")
    browser = AdsPower(configured.base_url, configured.api_key, profile_id)

    if command == "browser-open":
        await browser.ensure_started()
        print(f"AdsPower: профиль {profile_id} открыт", flush=True)
    elif command == "browser-close":
        await browser.stop_profile()
        print(f"AdsPower: профиль {profile_id} закрыт", flush=True)
    else:
        raise ValueError(f"Неизвестная команда управления AdsPower: {command}")
