"""Explain cash-volume timers from the saved state and the 24-hour journal."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_profiles import profile_prefix  # noqa: E402
from volume_policy import rolling_cash_purchases  # noqa: E402


def main() -> None:
    env = dotenv_values(ROOT / '.env.cash2')
    path = Path(env.get('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
        if not row:
            raise RuntimeError('Состояние cash2 не найдено')
        state = json.loads(row[0])
        print('Серия:', state.get('status'), 'активный цикл:',
              state.get('active_cycle') or 'нет', flush=True)
        journal = type('ReadOnlyJournal', (), {'db': db})()
        for name, timer in state.get('cooldowns', {}).items():
            nickname = env.get(profile_prefix(name) + '_NICKNAME') or name
            member_id = env.get(profile_prefix(name) + '_MEMBER_ID') or ''
            try:
                window = rolling_cash_purchases(journal, name, member_id=member_id)
                quantity = window['quantity']
                uncertain = window.get('uncertain_until') or 'нет'
            except (ValueError, ArithmeticError, sqlite3.Error) as exc:
                quantity, uncertain = type(exc).__name__, 'не удалось рассчитать'
            print(f'{nickname} ({name}): причина {timer.get("reason") or "не указана"}; '
                  f'до {timer.get("until")}; подтверждено за 24 ч {quantity} USDT; '
                  f'неопределённая сделка до {uncertain}', flush=True)


if __name__ == '__main__':
    main()
