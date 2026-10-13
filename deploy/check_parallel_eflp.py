"""Read-only preflight for two Eflp Telegram bots sharing one Sheets tab."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def value(env, key):
    return str(env.get(key) or '').strip()


def path(env, key, default):
    candidate = Path(value(env, key) or default)
    return (candidate if candidate.is_absolute() else ROOT / candidate).resolve()


def makers(env):
    return {key.strip().lower() for key in value(env, 'EFLP_P1_PROFILES').split(',') if key.strip()}


def saved_makers(db_path):
    if not db_path.exists():
        return set()
    with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
    if not row:
        return set()
    state = json.loads(row[0])
    if state.get('mode') not in {'eflp_volume', 'eflp_unique'} or state.get('status') == 'done':
        return set()
    return set(state.get('p1_profiles') or [state.get('p1_profile')]) - {None}


def main(files):
    if len(files) != 2:
        raise SystemExit('Использование: python deploy/check_parallel_eflp.py .env.eflp .env.eflp-unique')
    environments = [dotenv_values(filename) for filename in files]
    left, right = environments
    errors = []
    for filename, env in zip(files, environments):
        if not makers(env):
            errors.append(f'{filename}: пустой EFLP_P1_PROFILES')
        if value(env, 'GOOGLE_SHEET_SHARED_EFLP').lower() not in {'true', '1', 'yes'}:
            errors.append(f'{filename}: задайте GOOGLE_SHEET_SHARED_EFLP=true')
        if value(env, 'ADSPOWER_CLOSE_OTHER_PROFILES').lower() != 'false':
            errors.append(f'{filename}: задайте ADSPOWER_CLOSE_OTHER_PROFILES=false')
    for key in ('GOOGLE_SHEET_ID', 'GOOGLE_SHEET_TAB'):
        if not value(left, key) or value(left, key) != value(right, key):
            errors.append(f'{key}: у ботов должна быть одна непустая настройка')
    for key in ('TELEGRAM_BOT_TOKEN',):
        if not value(left, key) or not value(right, key) or value(left, key) == value(right, key):
            errors.append(f'{key}: у ботов должны быть разные непустые значения')
    if path(left, 'CYCLE_DB', 'data/cycles.sqlite3') == path(right, 'CYCLE_DB', 'data/cycles.sqlite3'):
        errors.append('CYCLE_DB: у ботов должны быть разные базы')
    if path(left, 'LOG_DIR', 'logs') == path(right, 'LOG_DIR', 'logs'):
        errors.append('LOG_DIR: у ботов должны быть разные каталоги')
    overlap = makers(left) & makers(right)
    overlap |= saved_makers(path(left, 'CYCLE_DB', 'data/cycles.sqlite3')) & makers(right)
    if overlap:
        errors.append('П1 пересекаются с настройками или сохранённой серией первого бота: '
                      + ', '.join(sorted(overlap)))
    if not value(right, 'UNIQUE_P2_PROFILES') and not value(right, 'EFLP_UNIQUE_P2_PROFILES'):
        errors.append(f'{files[1]}: задайте UNIQUE_P2_PROFILES')
    if errors:
        raise SystemExit('Проверка не пройдена:\n' + '\n'.join('- ' + error for error in errors))
    print('Проверка пройдена: разные П1 и базы, общая вкладка, отдельные Telegram-боты.')


if __name__ == '__main__':
    main(sys.argv[1:])
