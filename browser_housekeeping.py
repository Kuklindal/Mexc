"""Conservative cleanup of terminal order tabs left by abandoned cash cycles."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit


ORDER_NO = re.compile(r'd\d{15,25}')
FINAL_STATES = {'DONE', 'COMPLETED', 'CANCEL'}


def tab_order_no(target: dict) -> str | None:
    if target.get('type') != 'page':
        return None
    url = urlsplit(target.get('url', ''))
    if (url.scheme != 'https' or url.hostname not in {'mexc.com', 'www.mexc.com'}
            or not url.path.endswith('/buy-crypto/order-processing')):
        return None
    ids = parse_qs(url.query).get('id', [])
    return ids[0] if len(ids) == 1 and ORDER_NO.fullmatch(ids[0]) else None


def abandoned_orders(db: sqlite3.Connection) -> set[str]:
    """An order is eligible only when all its journal cycles are abandoned."""
    statuses: dict[str, set[str]] = defaultdict(set)
    for cycle_status, raw in db.execute("""SELECT c.status,s.result FROM steps s
        JOIN cycles c ON c.id=s.cycle_id
        WHERE s.name IN ('forward_create','reverse_create') AND s.status='done'"""):
        try:
            number = json.loads(raw or '{}').get('order_no')
        except (TypeError, ValueError):
            continue
        if isinstance(number, str) and ORDER_NO.fullmatch(number):
            statuses[number].add(cycle_status)
    return {number for number, values in statuses.items() if values == {'abandoned'}}


def is_final_order(detail: dict, number: str) -> bool:
    return (detail.get('advOrderNo', number) == number
            and detail.get('state') in FINAL_STATES)


async def cleanup_abandoned_cash_tabs(db, browser, client) -> tuple[int, int, int, int]:
    """Return open candidates, API-confirmed orders, closed tabs, skipped orders.

    No MEXC mutation is sent. Active and paused cycle tabs are never selected.
    """
    candidates = abandoned_orders(db)
    if not candidates:
        return 0, 0, 0, 0
    async with browser.connection() as call:
        targets = (await call('Target.getTargets')).get('targetInfos', [])
    open_candidates = sorted({number for target in targets
                              if (number := tab_order_no(target)) in candidates})
    confirmed = closed = skipped = 0
    for number in open_candidates:
        if number not in abandoned_orders(db):
            skipped += 1
            continue
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
            # A changed tab or an unavailable browser is left for the next run.
            skipped += 1
    return len(open_candidates), confirmed, closed, skipped
