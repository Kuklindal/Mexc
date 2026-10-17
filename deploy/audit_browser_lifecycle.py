"""Read-only inventory of AdsPower profiles across every bot configuration."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import dotenv_values

from inspect_open_browsers import browser_key, scheduler_state, service_state


ROOT = Path(__file__).resolve().parents[1]


def configs():
    for path in sorted(ROOT.glob('.env*')):
        if path.name != '.env' and (not path.name.startswith('.env.') or any(
                marker in path.name for marker in ('.example', '.sample', '.bak', '.backup', '.old'))):
            continue
        env = dotenv_values(path)
        if env.get('ADSPOWER_API_KEY'):
            yield path.name, env


def roles(state: dict) -> set[str]:
    pending = state.get('terminal_pending') or {}
    return {str(value) for value in (
        state.get('p1_profile'), state.get('current_profile'),
        pending.get('to'), pending.get('p2')) if value}


def main() -> None:
    groups = {}
    for label, env in configs():
        base = (env.get('ADSPOWER_BASE_URL') or 'http://127.0.0.1:50325').rstrip('/')
        key = (base, env['ADSPOWER_API_KEY'])
        group = groups.setdefault(key, {'labels': [], 'configured': defaultdict(list),
                                        'needed': defaultdict(list), 'paused': defaultdict(list),
                                        'check': set()})
        group['labels'].append(label)
        service = service_state(label)
        state = scheduler_state(env)
        status = state.get('status', 'нет состояния')
        print(f'{label}: служба {service}, серия {status}, '
              f'цикл {state.get("active_cycle") or "нет"}', flush=True)
        for name, profile_id in env.items():
            if name == 'ADSPOWER_P1_PROFILE_ID' or name.endswith('_ADSPOWER_PROFILE_ID'):
                if profile_id:
                    group['configured'][profile_id.strip()].append(f'{label}:{name}')
        if service != 'active':
            continue
        if not state:
            for profile_id in group['configured']:
                group['needed'][profile_id].append(f'{label}:состояние не прочитано')
            continue
        for role in roles(state):
            profile_id = (env.get(browser_key(role)) or '').strip()
            if not profile_id:
                continue
            if status in {'running', 'waiting'}:
                group['needed'][profile_id].append(f'{label}:{role}')
            elif status == 'paused':
                group['paused'][profile_id].append(f'{label}:{role}')
            group['check'].add(profile_id)

    if not groups:
        raise SystemExit('Настройки AdsPower не найдены')
    for number, ((base, api_key), group) in enumerate(groups.items(), 1):
        print(f'AdsPower-группа {number}: настройки {", ".join(group["labels"])}', flush=True)
        headers = {'Authorization': 'Bearer ' + api_key}
        try:
            with httpx.Client(timeout=12, trust_env=False) as client:
                response = client.get(base + '/api/v1/browser/local-active', headers=headers)
                body = response.json()
                data = body.get('data') if isinstance(body, dict) else None
                entries = data.get('list') if isinstance(data, dict) else None
                if response.status_code != 200 or body.get('code') != 0 or not isinstance(entries, list):
                    print('  Список не подтверждён AdsPower', flush=True)
                    continue
                opened = {item['user_id'] for item in entries
                          if isinstance(item, dict) and isinstance(item.get('user_id'), str)}
                print(f'  Локально открыто: {len(opened)}', flush=True)
                for profile_id in sorted(opened):
                    if group['needed'].get(profile_id):
                        label = 'НУЖЕН РАБОТАЮЩЕМУ БОТУ'
                        detail = group['needed'][profile_id]
                    elif group['paused'].get(profile_id):
                        label = 'БОТ НА ПАУЗЕ'
                        detail = group['paused'][profile_id]
                    elif group['configured'].get(profile_id):
                        label = 'СВОБОДЕН ПО ЖУРНАЛУ'
                        detail = group['configured'][profile_id]
                    else:
                        label = 'НЕИЗВЕСТНЫЙ'
                        detail = []
                    print(f'  {profile_id}: {label} {", ".join(detail)}', flush=True)
                # AdsPower may omit a browser from local-active even while its
                # per-profile endpoint reports Active. Surface that disagreement.
                for profile_id in sorted(group['check'] - opened):
                    try:
                        response = client.get(base + '/api/v1/browser/active',
                                              params={'user_id': profile_id}, headers=headers)
                        body = response.json()
                        status = (body.get('data') or {}).get('status') if body.get('code') == 0 else 'неизвестно'
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                        status = 'проверка не удалась'
                    print(f'  {profile_id}: отсутствует в списке, адресная проверка {status}', flush=True)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            print(f'  Список не прочитан: {type(exc).__name__}', flush=True)


if __name__ == '__main__':
    main()
