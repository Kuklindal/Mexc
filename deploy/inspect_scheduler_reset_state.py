"""Read cash bot scheduler state before any manual series reset."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def inspect(name: str) -> None:
    env_path = ROOT / name
    if not env_path.is_file():
        print(name, 'конфигурация не найдена')
        return
    env = dotenv_values(env_path)
    path = Path(env.get('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        print(name, 'журнал не найден')
        return
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
        unfinished = db.execute("""SELECT COUNT(*) FROM cycles
            WHERE status NOT IN ('completed','abandoned')""").fetchone()[0]
        state = json.loads(row[0]) if row else None
        active = state.get('active_cycle') if state else None
        cycle = db.execute('SELECT status,spec FROM cycles WHERE id=?', (active,)).fetchone() if active else None
        steps = db.execute("""SELECT name,status,result FROM steps WHERE cycle_id=?
            AND name IN ('forward_create','forward_complete','reverse_create',
                         'reverse_complete','reverse_replenish')""", (active,)).fetchall() if active else []
        last = db.execute("""SELECT step,status FROM events WHERE cycle_id=?
            ORDER BY id DESC LIMIT 1""", (active,)).fetchone() if active else None
    if not row:
        print(name, 'сохранённой серии нет', 'незавершённых циклов:', unfinished)
        return
    print(name, 'режим:', state.get('mode'), 'статус:', state.get('status'),
          'активный цикл:', state.get('active_cycle') or 'нет',
          'ожидается возврат:', bool(state.get('pending_return')),
          'переход П1:', bool(state.get('terminal_pending')),
          'незавершённых циклов:', unfinished,
          'неделя налички:', state.get('cash_unique_week_start') or 'нет',
          'неделя Eflp:', state.get('eflp_week_start') or 'нет')
    if cycle:
        spec = json.loads(cycle[1])
        print(' ', 'цикл:', cycle[0], 'П2:', spec.get('p2_profile'),
              'последний шаг:', tuple(last) if last else 'нет')
        for step_name, step_status, result in steps:
            data = json.loads(result)
            print(' ', step_name, step_status, 'ордер:', data.get('order_no') or 'нет',
                  'USDT:', data.get('quantity') or 'нет')


if __name__ == '__main__':
    for config in sys.argv[1:] or ('.env.cash2', '.env'):
        inspect(config)
