"""Name cash-unique profile collisions without displaying account identifiers."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path) -> dict[str, str]:
    """Read the one-line account settings without requiring project packages."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$', line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].strip()
        values[match.group(1)] = value
    return values


def profile_prefix(name: str) -> str:
    if name == 'p1':
        return 'MEXC_P1'
    if name.startswith('p1_'):
        return 'MEXC_' + name.upper()
    return 'MEXC_P2' if name == 'default' else 'MEXC_P2_' + name.upper()


def profile_keys(value: str) -> list[str]:
    return [key.strip().lower() for key in value.split(',') if key.strip()]


def collisions(env: dict[str, str], maker: str, buyers: list[str]) -> list[str]:
    seen_members: dict[str, str] = {}
    seen_keys: dict[str, str] = {}
    messages: list[str] = []
    def label(name: str) -> str:
        nickname = (env.get(f'{profile_prefix(name)}_NICKNAME') or '').strip()
        return f'{name} ({nickname})' if nickname else name

    for name in [maker, *buyers]:
        prefix = profile_prefix(name)
        for field, seen in (('MEMBER_ID', seen_members), ('API_KEY', seen_keys)):
            value = (env.get(f'{prefix}_{field}') or '').strip()
            if not value:
                messages.append(f'{label(name)}: не заполнено {prefix}_{field}')
            elif value in seen:
                messages.append(f'{label(seen[value])} и {label(name)}: одинаковый {field}')
            else:
                seen[value] = name
    return messages


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else
                os.environ.get('MEXC_ENV_FILE') or ROOT / '.env.cash2')
    env = read_env(path)
    buyers = profile_keys(env.get('UNIQUE_P2_PROFILES', ''))
    makers = profile_keys(env.get('CASH_UNIQUE_P1_PROFILES', ''))
    print('Конфигурация:', path.name, '| П2:', len(buyers))
    if not buyers or not makers:
        raise SystemExit('Не заполнены UNIQUE_P2_PROFILES или CASH_UNIQUE_P1_PROFILES')
    for maker in makers:
        print('П1', maker)
        messages = collisions(env, maker, buyers)
        for message in messages:
            print(' ', message)
        if not messages:
            print('  Совпадений MEMBER_ID и API-ключей нет')


if __name__ == '__main__':
    main()
