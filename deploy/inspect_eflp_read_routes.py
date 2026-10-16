"""Read-only check of the maker and taker API routes for the current Eflp cycle."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import ENV_FILE  # noqa: E402
from mexc_client import MexcAPIError, MexcP2PClient, MexcReadUnavailable  # noqa: E402
from trade_profiles import profile_prefix, settings_for_profile  # noqa: E402


async def probe(label: str, key: str, ad_no: str | None = None) -> None:
    settings = settings_for_profile(key)
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                           settings.recv_window, proxy_url=settings.proxy_url,
                           timeout_seconds=8)
    try:
        if ad_no:
            await client.get_ad(ad_no)
        else:
            await client.list_orders(limit=1)
        print(f'{label}: чтение API OK', flush=True)
    except MexcReadUnavailable as exc:
        print(f'{label}: {exc}', flush=True)
    except MexcAPIError as exc:
        print(f'{label}: ответ MEXC {type(exc).__name__}, '
              f'HTTP {exc.http_status}, код {exc.code}', flush=True)
    except Exception as exc:
        print(f'{label}: {type(exc).__name__}', flush=True)
    finally:
        await client.close()


async def main() -> None:
    load_dotenv(ENV_FILE, override=True)
    db_path = Path(os.getenv('CYCLE_DB') or 'data/cycles.sqlite3')
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    with sqlite3.connect(f'file:{db_path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
        if not row:
            raise SystemExit('Сохранённая серия не найдена')
        state = json.loads(row[0])
        if state.get('mode') not in {'eflp_unique', 'eflp_volume'}:
            raise SystemExit('Это не Eflp-бот')
        cycle_id = state.get('active_cycle')
        step = db.execute("SELECT status FROM steps WHERE cycle_id=? AND name='forward_create'",
                          (cycle_id,)).fetchone() if cycle_id else None
    maker = state['p1_profile']
    taker = state.get('current_profile')
    print(f'{ENV_FILE.name}: П1 {maker}, П2 {taker or "—"}, '
          f'цикл {cycle_id or "нет"}, создание {step[0] if step else "не начиналось"}',
          flush=True)
    ad_no = os.getenv(f'{profile_prefix(maker)}_SELL_ADV_NO')
    if not ad_no:
        raise SystemExit('SELL-объявление П1 не задано')
    await probe('П1 SELL', maker, ad_no)
    if taker:
        await probe('П2 история ордеров', taker)


if __name__ == '__main__':
    asyncio.run(main())
