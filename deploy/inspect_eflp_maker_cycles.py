"""Read the cycles after the last Eflp maker transfer without exposing order data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from dotenv import load_dotenv


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(root / '.env.eflp-unique')),
                override=True)
    path = Path(os.environ['CYCLE_DB'])
    if not path.is_absolute():
        path = root / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        state = json.loads(db.execute(
            "SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()[0])
        profile = state['p1_profile']
        transfer = db.execute("""SELECT c.rowid,c.id,s.result FROM cycles c
            JOIN steps s ON s.cycle_id=c.id AND s.name='reverse_complete' AND s.status='done'
            WHERE c.status='completed'
              AND json_extract(c.spec,'$.scheduler_mode')='eflp_unique'
              AND json_extract(c.spec,'$.reverse_p1_profile')=?
            ORDER BY c.rowid DESC LIMIT 1""", (profile,)).fetchone()
        if not transfer:
            raise SystemExit('Завершённый переход к текущему П1 не найден')
        print('Переход:', transfer['id'], 'П1:', profile,
              'получено USDT:', json.loads(transfer['result']).get('quantity'), flush=True)
        rows = db.execute("""SELECT c.rowid,c.id,c.status,c.spec FROM cycles c
            WHERE c.rowid>? AND json_extract(c.spec,'$.p1_profile')=?
            ORDER BY c.rowid LIMIT 12""", (transfer['rowid'], profile)).fetchall()
        if not rows:
            print('Циклов нового П1 после перехода нет', flush=True)
        for row in rows:
            steps = {s['name']: s for s in db.execute(
                'SELECT name,status,result FROM steps WHERE cycle_id=?', (row['id'],))}
            def value(name: str) -> str:
                step = steps.get(name)
                if not step:
                    return '—'
                result = json.loads(step['result'])
                quantity = result.get('quantity')
                return step['status'] + (f'/{quantity} USDT' if quantity is not None else '')
            print('Цикл:', row['id'], row['status'],
                  'покупка:', value('forward_create'),
                  'продажа:', value('forward_complete'),
                  'возврат:', value('reverse_complete'),
                  'SELL-пополнение:', value('reverse_replenish'), flush=True)


if __name__ == '__main__':
    main()
