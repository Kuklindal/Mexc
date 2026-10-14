"""Read the current Eflp unique maker's ad status and trading limits."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402
from trade_profiles import profile_prefix  # noqa: E402


async def main() -> None:
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.eflp-unique')),
                override=True)
    path = Path(os.environ['CYCLE_DB'])
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
    if not row:
        raise SystemExit('Состояние серии не найдено')
    state = json.loads(row[0])
    if state.get('mode') != 'eflp_unique':
        raise SystemExit('Это не журнал режима «Уникальные Eflp»')
    profile = state['p1_profile']
    prefix = profile_prefix(profile)
    print('П1', profile, 'серия', state.get('status'),
          'цикл', state.get('active_cycle') or 'нет', flush=True)
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("""SELECT c.id,c.status,s.result
            FROM cycles c JOIN steps s ON s.cycle_id=c.id
                AND s.name='reverse_complete' AND s.status='done'
            WHERE json_extract(c.spec,'$.scheduler_mode')='eflp_unique'
                AND json_extract(c.spec,'$.reverse_p1_profile')=?
            ORDER BY c.rowid DESC LIMIT 1""", (profile,)).fetchone()
    if row:
        print('Финальный переход:', row[0], 'цикл', row[1],
              'получено USDT', json.loads(row[2]).get('quantity'), flush=True)
    else:
        print('Финальный переход: не найден в этом журнале', flush=True)
    settings = Settings.from_env('p1', p1_profile=profile)
    client = MexcP2PClient(settings.api_key, settings.secret_key,
                          settings.base_url, settings.recv_window,
                          settings.proxy_url)
    try:
        with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
            last_refill = db.execute("""SELECT c.id,s.result FROM cycles c
                JOIN steps s ON s.cycle_id=c.id
                    AND s.name='reverse_replenish' AND s.status='done'
                WHERE json_extract(c.spec,'$.p1_profile')=?
                ORDER BY c.rowid DESC LIMIT 1""", (profile,)).fetchone()
        if last_refill:
            plan = json.loads(last_refill[1])
            recorded_ad = plan.get('adv_no')
            configured_ad = os.environ.get(f'{prefix}_SELL_ADV_NO', '').strip()
            print('Последний SELL:', last_refill[0], 'номер совпадает:',
                  recorded_ad == configured_ad, 'цель:',
                  plan.get('target_available'), 'USDT', flush=True)
            if recorded_ad and recorded_ad != configured_ad:
                print('Последний SELL относится к другому П1; проверка его остатка пропущена',
                      flush=True)
        for side in ('SELL', 'BUY'):
            ad_no = os.environ.get(f'{prefix}_{side}_ADV_NO', '').strip()
            if not ad_no:
                print(side, 'номер не задан', flush=True)
                continue
            try:
                ad = await client.get_ad(ad_no)
                print(side, 'статус', ad.get('advStatus'),
                      'остаток', ad.get('availableQuantity'),
                      'цена', ad.get('price'),
                      'лимиты', ad.get('minSingleTransAmount'),
                      '–', ad.get('maxSingleTransAmount'),
                      'валюта', ad.get('fiatUnit'), flush=True)
            except Exception as exc:
                print(side, 'ошибка', type(exc).__name__, flush=True)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
