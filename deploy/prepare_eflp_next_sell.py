"""Resume the missing SELL-ad refill after a completed Eflp maker switch."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eflp_ads import prepare_next_sell  # noqa: E402
from journal import Journal  # noqa: E402


async def main() -> None:
    active = subprocess.run(['systemctl', '--user', 'is-active',
                             'mexc-parallel@eflp-unique.service'],
                            capture_output=True, text=True, timeout=10).stdout.strip()
    if active not in {'inactive', 'failed'}:
        raise SystemExit('Сначала остановите mexc-parallel@eflp-unique.service')
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.eflp-unique')),
                override=True)
    path = Path(os.environ['CYCLE_DB'])
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        saved = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
        if not saved:
            raise SystemExit('Серия Eflp не найдена')
        state = json.loads(saved[0])
        if (state.get('mode') != 'eflp_unique' or state.get('status') != 'paused'
                or state.get('active_cycle') or state.get('terminal_pending')):
            raise SystemExit('Серия не на паузе перед первым циклом нового П1')
        profile = state['p1_profile']
        row = db.execute("""SELECT c.id FROM cycles c JOIN steps s ON s.cycle_id=c.id
                AND s.name='reverse_complete' AND s.status='done'
            WHERE c.status='completed'
                AND json_extract(c.spec,'$.scheduler_mode')='eflp_unique'
                AND json_extract(c.spec,'$.reverse_p1_profile')=?
            ORDER BY c.rowid DESC LIMIT 1""", (profile,)).fetchone()
        if not row:
            raise SystemExit('Завершённый переход к текущему П1 не найден')
        cycle_id = row[0]
        later = db.execute("""SELECT 1 FROM cycles c JOIN steps s ON s.cycle_id=c.id
                AND s.name='forward_create' AND s.status='done'
            WHERE c.rowid>(SELECT rowid FROM cycles WHERE id=?)
                AND json_extract(c.spec,'$.p1_profile')=? LIMIT 1""",
            (cycle_id, profile)).fetchone()
        if later:
            raise SystemExit('Новый П1 уже создавал ордер; автоматическое пополнение запрещено')
    journal = Journal(path)
    try:
        await prepare_next_sell(journal, cycle_id, profile)
        step = journal.step(cycle_id, 'eflp_next_sell_refill')
        print('SELL нового П1 подготовлен:', profile, 'действие',
              step['result'].get('action', 'пополнен'), flush=True)
    finally:
        journal.close()


if __name__ == '__main__':
    asyncio.run(main())
