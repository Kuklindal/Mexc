"""Restore a maker's offline Eflp advertisement through its own browser."""
from __future__ import annotations

import asyncio

from adspower import AdsPower, AdsPowerError, AdsPowerUnavailable


def maker_browser(profile, env) -> AdsPower:
    return AdsPower(env.get('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                    env.get('ADSPOWER_API_KEY', ''), profile.adspower_profile_id,
                    alternative_mexc_domains=True)


async def ensure_online(client, browser: AdsPower, ad: dict, ad_no: str,
                        side: str, fiat: str) -> dict:
    """Publish only a verified CLOSE ad; return a fresh public API snapshot."""
    if (ad.get('advNo') != ad_no or ad.get('side') != side
            or ad.get('coinName') != 'USDT' or ad.get('fiatUnit') != fiat):
        raise AdsPowerError(f'Объявление {ad_no} не соответствует П1, стороне или валюте')
    status = ad.get('advStatus')
    if status == 'OPEN':
        return ad
    if status != 'CLOSE':
        raise AdsPowerError(f'Объявление {ad_no}: статус {status}; автоматическая публикация недоступна')
    await browser.ensure_started()
    await browser.ensure_mexc_page()
    await browser.publish_ad(ad_no, side, fiat)
    for attempt in range(5):
        current = await client.get_ad(ad_no)
        if (current.get('advNo') != ad_no or current.get('side') != side
                or current.get('coinName') != 'USDT' or current.get('fiatUnit') != fiat):
            raise AdsPowerError(f'Объявление {ad_no} изменилось после публикации')
        if current.get('advStatus') == 'OPEN':
            return current
        if attempt < 4:
            await asyncio.sleep(2)
    raise AdsPowerUnavailable(f'Публикация объявления {ad_no} ещё не подтверждена MEXC; статус будет проверен снова')
