"""Close only cash2 order tabs whose abandoned orders are final on MEXC."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower  # noqa: E402
from browser_housekeeping import cleanup_abandoned_cash_tabs  # noqa: E402
from config import Settings  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402


async def main() -> None:
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.cash2')), override=True)
    path = Path(os.getenv('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    settings = Settings.from_env('p1')
    browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''), os.environ['ADSPOWER_P1_PROFILE_ID'])
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                          settings.recv_window, settings.proxy_url)
    try:
        with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
            found, confirmed, closed, skipped = await cleanup_abandoned_cash_tabs(
                db, browser, client)
    finally:
        await client.close()
    print('Вкладок из брошенных циклов:', found, flush=True)
    print('MEXC подтвердил конечный статус:', confirmed,
          'вкладок закрыто:', closed, 'оставлено для проверки:', skipped, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
