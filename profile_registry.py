"""Add a named P2 account to .env without printing its credentials."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from dotenv import set_key

from config import p2_prefix, p2_profile_name, proxy_url


def add_profile(path: Path, values: list[str], env=os.environ) -> str:
    if len(values) not in {6, 7}:
        raise ValueError('Формат: /addprofile ИМЯ API_KEY SECRET_KEY MEMBER_ID НИК PAYMENT_ID [PROXY_URL]')
    name = p2_profile_name(values[0])
    if name == 'default':
        raise ValueError('Имя default уже закреплено за первым П2')
    prefix = p2_prefix(name)
    fields = dict(zip(('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME', 'PAYMENT_ID'), values[1:]))
    if len(values) == 7:
        fields['PROXY_URL'] = proxy_url(values[6], prefix + '_PROXY_URL') or ''
    if any(not value or len(value) > 256 or any(c.isspace() or c in "'\\" for c in value)
           for value in fields.values()):
        raise ValueError('Поля должны быть без пробелов, одинарных кавычек и обратного слэша')
    if not fields['PAYMENT_ID'].isdigit() or int(fields['PAYMENT_ID']) <= 0:
        raise ValueError('PAYMENT_ID — положительный ID реквизитов П2, не код способа оплаты 578')
    current = [part.strip() for part in env.get('ROLLOVER_PROFILES', 'default').split(',')]
    if name in current:
        raise ValueError('Такое имя профиля уже есть')
    if any(env.get(prefix + '_' + field) for field in fields):
        raise ValueError('Поля этого профиля уже заполнены в .env')
    if fields['API_KEY'] in {env.get('MEXC_P1_API_KEY'), *(env.get(p2_prefix(n) + '_API_KEY') for n in current)}:
        raise ValueError('API-ключ уже используется другим участником')
    if fields['MEMBER_ID'] in {env.get('MEXC_P1_MEMBER_ID'), *(env.get(p2_prefix(n) + '_MEMBER_ID') for n in current)}:
        raise ValueError('ID участника уже используется другим профилем')
    if not path.is_file():
        raise ValueError('.env не найден')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.env-profile-', delete=False) as opened:
            temporary = Path(opened.name)
            opened.write(path.read_bytes())
        for field, value in fields.items():
            set_key(str(temporary), prefix + '_' + field, value, quote_mode='always')
        set_key(str(temporary), 'ROLLOVER_PROFILES', ','.join(current + [name]), quote_mode='never')
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    for field, value in fields.items():
        env[prefix + '_' + field] = value
    env['ROLLOVER_PROFILES'] = ','.join(current + [name])
    return name
