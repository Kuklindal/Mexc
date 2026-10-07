"""Read payment account IDs from selected local AdsPower profiles without orders."""
from __future__ import annotations

import os
import re

from adspower import AdsPower, AdsPowerError
from config import p2_profile_name
from trade_profiles import mode_pay_method_id, profile_prefix


def selected_profiles(raw: str) -> list[tuple[str, str]]:
    profiles = []
    for part in re.split(r'[,\s]+', raw.strip()):
        if not part:
            continue
        key, separator, browser_id = part.partition('=')
        profile = p2_profile_name(key)
        if separator and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', browser_id):
            raise ValueError(f'П2 {profile}: после = укажите ID профиля AdsPower')
        profiles.append((profile, browser_id))
    if not profiles:
        raise ValueError('Укажите хотя бы один профиль П2 через --profiles')
    if len(profiles) != len({profile for profile, _ in profiles}):
        raise ValueError('--profiles: повторяются ключи П2')
    return profiles


async def read_selected_payment_ids(args, env=os.environ) -> int:
    """Visit profiles sequentially; only print IDs, never modify .env or place orders."""
    profiles = selected_profiles(args.profiles)
    fiat = (args.fiat or ('RUB' if args.mode == 'cash' else '')).strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', fiat):
        raise ValueError('--fiat: укажите три латинские буквы валюты, например KZT')
    if args.mode == 'cash' and fiat != 'RUB':
        raise ValueError('Для режима налички укажите RUB; для другой валюты используйте --mode eflp')
    mode = 'cash_volume' if args.mode == 'cash' else 'eflp_volume'
    method = int(mode_pay_method_id(mode, fiat, env))
    api_key = (env.get('ADSPOWER_API_KEY') or '').strip()
    if not api_key:
        raise ValueError('В выбранном .env не задан ADSPOWER_API_KEY')
    browser_ids = {}
    selected = []
    for profile, override in profiles:
        field = f'{profile_prefix(profile)}_ADSPOWER_PROFILE_ID'
        browser_id = override or (env.get(field) or '').strip()
        if not browser_id:
            raise ValueError(f'{field}: задайте в .env или укажите {profile}=ID в --profiles')
        if browser_id in browser_ids:
            raise ValueError(f'{field}: тот же профиль AdsPower уже указан для П2 {browser_ids[browser_id]}')
        browser_ids[browser_id] = profile
        selected.append((profile, browser_id))
    failures = 0
    base_url = env.get('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325')
    print(f'Чтение реквизитов: {fiat}, payMethod {method}; профилей: {len(profiles)}', flush=True)
    for profile, browser_id in selected:
        browser = AdsPower(base_url, api_key, browser_id)
        try:
            account_id = await browser.payment_account_by_method(method, fiat)
        except AdsPowerError as exc:
            failures += 1
            print(f'П2 {profile}: ошибка — {exc}', flush=True)
            continue
        if args.mode == 'cash':
            print(f'{profile_prefix(profile)}_PAYMENT_ID={account_id}', flush=True)
        else:
            print(f'{profile_prefix(profile)}_PAYMENT_ID_{fiat}={account_id}', flush=True)
    print(f'Готово: {len(profiles) - failures} из {len(profiles)}', flush=True)
    return 1 if failures else 0
