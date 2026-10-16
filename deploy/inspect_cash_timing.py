"""Show recent cash2 cycle timing and pauses without order or account details."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
KRS = timezone(timedelta(hours=7))


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(KRS)


def stamp(value: datetime) -> str:
    return value.strftime('%d.%m %H:%M:%S')


def main() -> None:
    env = dotenv_values(ROOT / '.env.cash2')
    db_path = Path(env.get('CYCLE_DB') or 'data/cycles.sqlite3')
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    with sqlite3.connect(f'file:{db_path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
        state = json.loads(row[0]) if row else {}
        rows = db.execute("""SELECT id,status,spec FROM cycles
            WHERE json_extract(spec,'$.scheduler_mode')='cash_volume'
            ORDER BY rowid DESC LIMIT 25""").fetchall()
        print('Серия:', state.get('status'), 'активный цикл:',
              state.get('active_cycle') or 'нет', flush=True)
        action_low = env.get('ACTION_DELAY_MIN_SECONDS') or env.get('ACTION_DELAY_SECONDS') or '20'
        action_high = env.get('ACTION_DELAY_MAX_SECONDS') or env.get('ACTION_DELAY_SECONDS') or '20'
        print('Паузы действий:', action_low, '–', action_high, 'секунд', flush=True)
        previous_end = None
        for cid, status, raw_spec in reversed(rows):
            events = [(moment(time), step, event_status) for time, step, event_status in
                      db.execute('SELECT time,step,status FROM events WHERE cycle_id=? ORDER BY id',
                                 (cid,)).fetchall()]
            if not events:
                continue
            first, last = events[0][0], events[-1][0]
            spec = json.loads(raw_spec)
            duration = round((last - first).total_seconds() / 60, 1)
            gap = round((first - previous_end).total_seconds() / 60, 1) if previous_end else None
            print('Цикл', cid, status, stamp(first), '→', stamp(last),
                  'длительность', duration, 'мин',
                  'ожидание до цикла', gap if gap is not None else '—', 'мин',
                  'таймеры оплаты/выдачи',
                  spec.get('cash_wait_min_seconds', 75), '–',
                  spec.get('cash_wait_max_seconds', 105), 'сек', flush=True)
            long_steps = [(round((second[0] - first_event[0]).total_seconds()),
                           first_event[1], first_event[2], second[1], second[2])
                          for first_event, second in zip(events, events[1:])
                          if (second[0] - first_event[0]).total_seconds() >= 90]
            for seconds, old_step, old_status, new_step, new_status in long_steps[:3]:
                print('  пауза', seconds, 'сек:', old_step, old_status,
                      '→', new_step, new_status, flush=True)
            previous_end = last


if __name__ == '__main__':
    main()
