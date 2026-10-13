"""Summarize saved bot steps and AdsPower availability without changing either."""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from pathlib import Path

import httpx
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
INSTANCES = ('cash2', 'eflp-unique', 'eflp-unique2')


def browser_id(env: dict, name: str | None) -> str:
    if not name:
        return ''
    key = ('ADSPOWER_P1_PROFILE_ID' if name == 'p1' else
           f'MEXC_{name.upper()}_ADSPOWER_PROFILE_ID')
    return (env.get(key) or '').strip()


def browser_state(client: httpx.Client, base: str, headers: dict,
                  pid: str) -> str:
    if not pid:
        return 'ID не задан'
    try:
        response = client.get(base + '/api/v1/browser/active',
                              params={'user_id': pid}, headers=headers)
        body = response.json()
        data = body.get('data') or {}
        port = data.get('debug_port')
        try:
            with socket.create_connection(('127.0.0.1', int(port)), timeout=2):
                tcp = 'да'
        except (OSError, TypeError, ValueError):
            tcp = 'нет'
        return f"API {body.get('code')} {data.get('status')} TCP {tcp}"
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return type(exc).__name__


def main() -> None:
    for label in INSTANCES:
        path = ROOT / f'.env.{label}'
        if not path.is_file():
            print(label, 'нет файла настроек', flush=True)
            continue
        env = dotenv_values(path)
        db_path = Path(env.get('CYCLE_DB') or 'data/cycles.sqlite3')
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        with sqlite3.connect(f'file:{db_path.resolve().as_posix()}?mode=ro', uri=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
            if not row:
                print(label, 'состояние серии не найдено', flush=True)
                continue
            state = json.loads(row[0])
            cycle_id = state.get('active_cycle')
            event = db.execute(
                'SELECT step,status,order_no FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT 1',
                (cycle_id,)).fetchone() if cycle_id else None
        unit = f'mexc-parallel@{label}.service'
        try:
            service = subprocess.run(['systemctl', '--user', 'is-active', unit],
                                     capture_output=True, text=True, timeout=8).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            service = 'неизвестно'
        pending = state.get('terminal_pending') or {}
        print(f'{label}: служба {service}; режим {state.get("mode")}; '
              f'серия {state.get("status")}; П1 {state.get("p1_profile")}; '
              f'цикл {cycle_id or "нет"}; переход '
              f'{pending.get("from") or "—"} → {pending.get("to") or "—"}', flush=True)
        print('  шаг:', ' | '.join(str(value) for value in event) if event else 'нет',
              '| ошибка:', str(state.get('last_error') or '—')[:180], flush=True)
        base = (env.get('ADSPOWER_BASE_URL') or 'http://127.0.0.1:50325').rstrip('/')
        headers = {'Authorization': 'Bearer ' + (env.get('ADSPOWER_API_KEY') or '')}
        with httpx.Client(timeout=8, trust_env=False) as client:
            for role, name in (('П1', state.get('p1_profile')),
                               ('следующий П1', pending.get('to'))):
                if not name:
                    continue
                pid = browser_id(env, name)
                print(f'  {role} {name}: AdsPower {pid or "—"} '
                      f'{browser_state(client, base, headers, pid)}', flush=True)


if __name__ == '__main__':
    main()
