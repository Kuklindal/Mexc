"""Resolve configured MEXC accounts independently from their trade role."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from config import Settings, p2_prefix, p2_profile_name


@dataclass(frozen=True)
class TradeProfile:
    key: str
    prefix: str
    nickname: str
    member_id: str
    sell_adv_no: str
    adspower_profile_id: str


def profile_prefix(key: str) -> str:
    if key == 'p1':
        return 'MEXC_P1'
    if re.fullmatch(r'p1_[a-z0-9_]{1,29}', key):
        return 'MEXC_' + key.upper()
    return p2_prefix(p2_profile_name(key))


def eflp_p1_fiat(key: str, env: Mapping[str, str]) -> str:
    """Fiat assigned to an Eflp maker; it must match both live advertisements."""
    field = f'{profile_prefix(key)}_FIAT'
    fiat = (env.get(field) or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', fiat):
        raise ValueError(f'{field}: укажите три латинские буквы валюты, например GEL')
    return fiat


def eflp_p2_payment_id(key: str, fiat: str, env: Mapping[str, str]) -> str:
    """Account-specific collection method for the selected Eflp fiat."""
    field = f'{profile_prefix(key)}_PAYMENT_ID_{fiat}'
    payment_id = (env.get(field) or '').strip()
    if not payment_id.isascii() or not payment_id.isdecimal() or int(payment_id or '0') <= 0:
        raise ValueError(f'{field}: укажите активный положительный ID реквизитов П2 для {fiat}')
    return payment_id


def mode_pay_method_id(mode: str, fiat: str, env: Mapping[str, str]) -> str:
    """Payment type for cash's first order or both Eflp order participants."""
    if mode == 'cash_volume':
        field = 'MEXC_PAY_METHOD_ID'
        value = (env.get(field) or '578').strip()
    elif mode in {'eflp_volume', 'eflp_unique'}:
        field = f'EFLP_PAY_METHOD_ID_{fiat}'
        value = (env.get(field) or '').strip()
    else:
        raise ValueError('Неизвестный режим для способа оплаты')
    if not value.isascii() or not value.isdecimal() or int(value or '0') <= 0:
        raise ValueError(f'{field}: укажите положительный числовой payMethod для {fiat}')
    return value


def eflp_p1_profiles(env: Mapping[str, str]) -> list[str]:
    """Dedicated Eflp maker accounts; never infer them from the P2 pool."""
    raw = (env.get('EFLP_P1_PROFILES') or 'p1').strip()
    keys = [part.strip().lower() for part in raw.split(',')]
    if (not all(key == 'p1' or re.fullmatch(r'p1_[a-z0-9_]{1,29}', key)
                for key in keys) or len(keys) != len(set(keys))):
        raise ValueError('EFLP_P1_PROFILES: укажите разные ключи p1,p1_2,p1_3 через запятую')
    return keys


def settings_for_profile(key: str) -> Settings:
    """Resolve a saved account key, including legacy P2 accounts used as P1."""
    if key == 'p1' or re.fullmatch(r'p1_[a-z0-9_]{1,29}', key):
        return Settings.from_env('p1', p1_profile=key)
    return Settings.from_env('p2', p2_profile=key)


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


def ready_p1_profiles(p2_names: list[str], env: Mapping[str, str], *,
                      include_main: bool = True) -> list[TradeProfile]:
    """Show only accounts with credentials, a sell ad and an AdsPower profile."""
    ready = []
    for key in (['p1', *p2_names] if include_main else p2_names):
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
