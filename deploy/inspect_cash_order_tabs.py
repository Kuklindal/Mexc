"""Count P1 order tabs from completed cash2 cycles without showing order IDs."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower


ORDER_NO = re.compile(r'd\d{15,25}')


def known_orders(db: sqlite3.Connection) -> tuple[set[str], set[str], dict[str, set[str]]]:
    completed, unfinished = set(), set()
    statuses = defaultdict(set)
    rows = db.execute("""SELECT c.status, s.result FROM steps s
        JOIN cycles c ON c.id=s.cycle_id
        WHERE s.name IN ('forward_create','reverse_create') AND s.status='done'""")
    for status, raw in rows:
        try:
            order_no = json.loads(raw or '{}').get('order_no')
        except (TypeError, ValueError):
            continue
        if not isinstance(order_no, str) or not ORDER_NO.fullmatch(order_no):
            continue
        (completed if status == 'completed' else unfinished).add(order_no)
        statuses[order_no].add(status)
    return completed - unfinished, unfinished, statuses


def tab_order_no(target: dict) -> str | None:
    if target.get('type') != 'page':
        return None
    url = urlsplit(target.get('url', ''))
    if (url.scheme != 'https' or url.hostname not in {'mexc.com', 'www.mexc.com'}
            or not url.path.endswith('/buy-crypto/order-processing')):
        return None
    ids = parse_qs(url.query).get('id', [])
    return ids[0] if len(ids) == 1 and ORDER_NO.fullmatch(ids[0]) else None


async def main() -> None:
    load_dotenv(ROOT / '.env.cash2', override=True)
    path = Path(os.getenv('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        completed, unfinished, statuses = known_orders(db)
    browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''),
                       os.environ['ADSPOWER_P1_PROFILE_ID'])
    async with browser.connection() as call:
        targets = (await call('Target.getTargets')).get('targetInfos', [])
    pages = [target for target in targets if target.get('type') == 'page']
    kinds = Counter()
    for target in pages:
        url = urlsplit(target.get('url', ''))
        if url.hostname not in {'mexc.com', 'www.mexc.com', 'mexc.co', 'www.mexc.co',
                                'mexc.io', 'www.mexc.io'}:
            kinds['другие'] += 1
        elif url.path.endswith('/buy-crypto/control'):
            kinds['портал'] += 1
        elif url.path.endswith('/buy-crypto/order-processing'):
            kinds['ордера'] += 1
        else:
            kinds['другие MEXC'] += 1
    print('Страниц П1:', len(pages),
          'портал:', kinds['портал'], 'ордера:', kinds['ордера'],
          'другие MEXC:', kinds['другие MEXC'], 'другие:', kinds['другие'])
    tabs = [number for target in targets if (number := tab_order_no(target))]
    print('Вкладок ордеров П1:', len(tabs),
          'из завершённых циклов:', sum(number in completed for number in tabs),
          'из незавершённых:', sum(number in unfinished for number in tabs),
          'неизвестных:', sum(number not in completed | unfinished for number in tabs))
    cycle_statuses = Counter(status for number in tabs for status in statuses.get(number, {'нет в журнале'}))
    print('Статусы циклов открытых вкладок:',
          ', '.join(f'{status} {count}' for status, count in sorted(cycle_statuses.items())) or 'вкладок нет')


if __name__ == '__main__':
    asyncio.run(main())
