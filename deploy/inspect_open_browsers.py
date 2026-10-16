"""List locally open AdsPower profiles and the bot roles that currently need them."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower  # noqa: E402


def browser_key(role: str) -> str:
    if role == 'p1':
        return 'ADSPOWER_P1_PROFILE_ID'
    if role.startswith('p1_'):
        return f'MEXC_{role.upper()}_ADSPOWER_PROFILE_ID'
    if role == 'default':
        return 'MEXC_P2_ADSPOWER_PROFILE_ID'
    return f'MEXC_P2_{role.upper()}_ADSPOWER_PROFILE_ID'


def service_state(label: str) -> str:
    unit = ('mexc-telegram.service' if label == '.env' else
            f'mexc-parallel@{label.removeprefix(".env.")}.service')
    try:
        result = subprocess.run(['systemctl', '--user', 'is-active', unit],
                                capture_output=True, text=True, timeout=6)
        return result.stdout.strip() or 'неизвестно'
    except (OSError, subprocess.TimeoutExpired):
        return 'неизвестно'


def scheduler_state(env: dict) -> dict:
    path = Path(env.get('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    try:
        with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
            if not row:
                return {}
            state = json.loads(row[0])
            if state.get('active_cycle') and not state.get('current_profile'):
                cycle = db.execute('SELECT spec FROM cycles WHERE id=?',
                                   (state['active_cycle'],)).fetchone()
                if cycle:
                    state['current_profile'] = json.loads(cycle[0]).get('p2_profile')
            return state
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--close', metavar='PROFILE_ID',
                        help='Close one currently idle, configured AdsPower profile')
    args = parser.parse_args()
    files = [path for path in sorted(ROOT.glob('.env*'))
             if path.name == '.env' or (path.name.startswith('.env.')
                 and not any(marker in path.name for marker in
                             ('.example', '.sample', '.bak', '.backup', '.old')))]
    configs = [(path.name, dotenv_values(path)) for path in files]
    configs = [(label, env) for label, env in configs if env.get('ADSPOWER_API_KEY')]
    if not configs:
        raise SystemExit('Настройки AdsPower не найдены')
    configured = defaultdict(list)
    needed = defaultdict(list)
    for label, env in configs:
        service = service_state(label)
        state = scheduler_state(env)
        for key, value in env.items():
            if (key == 'ADSPOWER_P1_PROFILE_ID' or key.endswith('_ADSPOWER_PROFILE_ID')) and value:
                configured[value.strip()].append(f'{label}:{key}')
        if service != 'active':
            continue
        roles = []
        if not state:
            # An unreadable state is not evidence that any configured browser is idle.
            for key, value in env.items():
                if (key == 'ADSPOWER_P1_PROFILE_ID' or key.endswith('_ADSPOWER_PROFILE_ID')) and value:
                    needed[value.strip()].append(f'{label}:состояние не прочитано')
        elif state.get('status') not in {'done', 'stopped'}:
            roles.extend([state.get('p1_profile'), state.get('current_profile')])
            pending = state.get('terminal_pending') or {}
            roles.extend([pending.get('p2'), pending.get('to')])
        for role in set(filter(None, roles)):
            profile_id = (env.get(browser_key(str(role))) or '').strip()
            if profile_id:
                needed[profile_id].append(f'{label}:{role}')
        print(f'Бот {label}: служба {service}, серия {state.get("status", "—")}, '
              f'П1 {state.get("p1_profile", "—")}, П2 {state.get("current_profile", "—")}, '
              f'цикл {state.get("active_cycle") or "нет"}', flush=True)
    source = configs[0][1]
    base = (source.get('ADSPOWER_BASE_URL') or 'http://127.0.0.1:50325').rstrip('/')
    headers = {'Authorization': 'Bearer ' + source['ADSPOWER_API_KEY']}
    with httpx.Client(timeout=15, trust_env=False) as client:
        response = client.get(base + '/api/v1/browser/local-active', headers=headers)
        body = response.json()
    data = body.get('data') if isinstance(body, dict) else None
    entries = data.get('list') if isinstance(data, dict) else None
    code = body.get('code') if isinstance(body, dict) else 'не JSON-объект'
    if response.status_code != 200 or code != 0 or not isinstance(entries, list):
        raise SystemExit(f'AdsPower не вернул список: HTTP {response.status_code}, код {code}')
    if any(not isinstance(entry, dict) or not isinstance(entry.get('user_id'), str)
           or not entry['user_id'] for entry in entries):
        raise SystemExit('AdsPower вернул неполный список; кандидаты не определены')
    ids = sorted({entry['user_id'] for entry in entries})
    unknown_names = {}
    with httpx.Client(timeout=8, trust_env=False) as client:
        for profile_id in ids:
            if profile_id in configured:
                continue
            try:
                details = client.get(base + '/api/v1/user/list',
                    params={'user_id': profile_id}, headers=headers).json()
                rows = (details.get('data') or {}).get('list') or []
                if details.get('code') == 0 and len(rows) == 1:
                    unknown_names[profile_id] = str(rows[0].get('name') or 'без имени')
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                pass
    print(f'Открыто профилей AdsPower: {len(ids)}', flush=True)
    for profile_id in ids:
        role = ('НУЖЕН СЕЙЧАС' if needed.get(profile_id) else
                'КАНДИДАТ' if configured.get(profile_id) else 'НЕИЗВЕСТНЫЙ')
        labels = ', '.join(needed.get(profile_id) or configured.get(profile_id) or [])
        if not labels and profile_id in unknown_names:
            labels = f'имя в AdsPower: {unknown_names[profile_id]}'
        print(f'{profile_id}: {role} {labels}', flush=True)
    if args.close:
        profile_id = args.close
        if profile_id not in ids:
            print(f'{profile_id}: уже закрыт', flush=True)
            return
        if profile_id not in configured or needed.get(profile_id):
            raise SystemExit(f'{profile_id}: закрытие запрещено: профиль неизвестен или нужен боту')
        browser = AdsPower(base, source['ADSPOWER_API_KEY'], profile_id)
        # Under heavy I/O pressure the preliminary per-profile /active request
        # can time out before /stop is sent. The inventory and role checks above
        # already establish that this exact open profile may be closed.
        asyncio.run(browser.stop_profile())
        print(f'{profile_id}: AdsPower принял запрос закрытия; проверьте повторным списком', flush=True)


if __name__ == '__main__':
    main()
