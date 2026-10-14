"""Show Eflp unique account overlaps without printing credentials or member IDs."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_modes import configured_mode_profiles  # noqa: E402
from trade_profiles import eflp_p1_profiles, profile_prefix, split_eflp_p2_profiles  # noqa: E402


def main() -> None:
    path = Path(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.eflp-unique')))
    env = dotenv_values(path)
    _, requested = configured_mode_profiles('eflp_unique', env)
    print('Конфигурация:', path.name)
    for maker in eflp_p1_profiles(env):
        selected, skipped = split_eflp_p2_profiles(maker, requested, env)
        print('П1', maker, '| пропущены как этот же аккаунт:', ', '.join(skipped) or 'нет')
        members: dict[str, str] = {}
        keys: dict[str, str] = {}
        duplicate = False
        for name in selected:
            prefix = profile_prefix(name)
            member = (env.get(f'{prefix}_MEMBER_ID') or '').strip()
            api_key = (env.get(f'{prefix}_API_KEY') or '').strip()
            if member and member in members:
                print('  П2', members[member], 'и', name, 'повторяют MEMBER_ID')
                duplicate = True
            elif member:
                members[member] = name
            if api_key and api_key in keys:
                print('  П2', keys[api_key], 'и', name, 'повторяют API-ключ')
                duplicate = True
            elif api_key:
                keys[api_key] = name
        if not duplicate:
            print('  Повторов среди оставшихся П2 нет')


if __name__ == '__main__':
    main()
