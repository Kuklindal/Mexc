"""Resolve configured MEXC accounts independently from their trade role."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from config import p2_prefix, p2_profile_name


@dataclass(frozen=True)
class TradeProfile:
    key: str
    prefix: str
    nickname: str
    member_id: str
    sell_adv_no: str
    adspower_profile_id: str


def profile_prefix(key: str) -> str:
    return 'MEXC_P1' if key == 'p1' else p2_prefix(p2_profile_name(key))


def profile_from_env(key: str, env: Mapping[str, str]) -> TradeProfile:
    prefix = profile_prefix(key)
    ads_field = ('ADSPOWER_P1_PROFILE_ID' if key == 'p1'
                 else f'{prefix}_ADSPOWER_PROFILE_ID')
    required = ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'SELL_ADV_NO')
    if key != 'p1':
        required += ('NICKNAME',)
    missing = [f'{prefix}_{field}' for field in required if not (env.get(f'{prefix}_{field}') or '').strip()]
    if not (env.get(ads_field) or '').strip():
        missing.append(ads_field)
    if missing:
        raise ValueError('Профиль не готов для роли мейкера: ' + ', '.join(missing))
    ad_no = env[f'{prefix}_SELL_ADV_NO'].strip()
    if not re.fullmatch(r'a\d{15,25}', ad_no):
        raise ValueError(f'{prefix}_SELL_ADV_NO: неверный номер объявления')
    return TradeProfile(key, prefix, (env.get(f'{prefix}_NICKNAME') or key).strip(),
                        env[f'{prefix}_MEMBER_ID'].strip(), ad_no,
                        env[ads_field].strip())


def ready_p1_profiles(p2_names: list[str], env: Mapping[str, str]) -> list[TradeProfile]:
    """Show only accounts with credentials, a sell ad and an AdsPower profile."""
    ready = []
    for key in ['p1', *p2_names]:
        try:
            ready.append(profile_from_env(key, env))
        except ValueError:
            pass
    return ready


def validate_unique_profiles(p1_key: str, p2_keys: list[str],
                             env: Mapping[str, str], minimum: int = 20) -> tuple[TradeProfile, list[TradeProfile]]:
    if len(p2_keys) < minimum or len(set(p2_keys)) != len(p2_keys) or p1_key in p2_keys:
        raise ValueError(f'Нужно выбрать не менее {minimum} разных П2, не включая выбранного П1')
    p1 = profile_from_env(p1_key, env)
    p2 = [profile_from_env(key, env) for key in p2_keys]
    identities = [p1.member_id, *(profile.member_id for profile in p2)]
    if len(identities) != len(set(identities)):
        raise ValueError('MEXC MEMBER_ID участников должны быть разными')
    browsers = [p1.adspower_profile_id, *(profile.adspower_profile_id for profile in p2)]
    if len(browsers) != len(set(browsers)):
        raise ValueError('Для разных аккаунтов нужны разные профили AdsPower')
    return p1, p2
