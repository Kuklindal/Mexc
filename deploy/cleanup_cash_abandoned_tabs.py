"""Close only cash2 order tabs whose abandoned orders are final on MEXC."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower  # noqa: E402
from config import Settings  # noqa: E402
from deploy.inspect_cash_order_tabs import tab_order_no  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402


FINAL_STATES = {'DONE', 'COMPLETED', 'CANCEL'}


def is_final_order(detail: dict, number: str) -> bool:
    return (detail.get('advOrderNo', number) == number
            and detail.get('state') in FINAL_STATES)


def abandoned_orders(db: sqlite3.Connection) -> set[str]:
    """Exclude an order if any journal record still needs its browser tab."""
    statuses: dict[str, set[str]] = defaultdict(set)
    for cycle_status, raw in db.execute("""SELECT c.status,s.result FROM steps s
        JOIN cycles c ON c.id=s.cycle_id
        WHERE s.name IN ('forward_create','reverse_create') AND s.status='done'"""):
        try:
            number = json.loads(raw or '{}').get('order_no')
        except (TypeError, ValueError):
            continue
        if isinstance(number, str):
            statuses[number].add(cycle_status)
    return {number for number, values in statuses.items() if values == {'abandoned'}}


async def main() -> None:
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.cash2')), override=True)
    path = Path(os.getenv('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        candidates = abandoned_orders(db)

    browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''), os.environ['ADSPOWER_P1_PROFILE_ID'])
    async with browser.connection() as call:
        targets = (await call('Target.getTargets')).get('targetInfos', [])
    open_candidates = sorted({number for target in targets
                              if (number := tab_order_no(target)) in candidates})
    print('Вкладок из брошенных циклов:', len(open_candidates), flush=True)
    if not open_candidates:
        return

    settings = Settings.from_env('p1')
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                          settings.recv_window, settings.proxy_url)
    confirmed, closed, skipped = 0, 0, 0
    try:
        for number in open_candidates:
            try:
                detail = await client.get_order_detail(number)
            except Exception:
                skipped += 1
                continue
            if not is_final_order(detail, number):
                skipped += 1
                continue
            confirmed += 1
            try:
                closed += await browser.close_order_tabs([number])
            except Exception:
                # The tab may have moved or AdsPower may be temporarily unavailable.
                skipped += 1
    finally:
        await client.close()
    print('MEXC подтвердил конечный статус:', confirmed,
          'вкладок закрыто:', closed, 'оставлено для проверки:', skipped, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
