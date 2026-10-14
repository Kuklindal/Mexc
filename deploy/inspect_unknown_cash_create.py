"""Read MEXC order history for one cash2 order-creation attempt with no saved order number."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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


async def inspect(client: MexcP2PClient, label: str, plan: dict,
                  start_ms: int, end_ms: int,
                  control: tuple[str, int, int] | None = None) -> None:
    try:
        orders = await client.list_orders(start_time_ms=start_ms, end_time_ms=end_ms, limit=50)
        if len(orders) >= 50:
            print(label, 'история содержит 50 ордеров; поиск неполный')
            return
        key = 'amount' if plan.get('amount') else 'tradableQuantity'
        expected = Decimal(str(plan.get('amount') or plan['tradable_quantity']))
        matching = []
        for order in orders:
            if (order.get('advNo') == plan['adv_no']
                    and Decimal(str(order.get(key))) == expected):
                matching.append(str(order.get('advOrderNo') or 'номер не указан'))
        print(label, 'всего в интервале:', len(orders), 'совпадений:', len(matching))
        for order_no in matching:
            if order_no == 'номер не указан':
                print(label, 'совпадение без номера; требуется ручная сверка')
                continue
            detail = await client.get_order_detail(order_no)
            print(label, 'ордер:', order_no, 'статус:', detail.get('state'))
        if control:
            known_no, known_start, known_end = control
            known_orders = await client.list_orders(start_time_ms=known_start,
                                                    end_time_ms=known_end, limit=50)
            if len(known_orders) >= 50:
                print(label, 'контрольная история неполная: 50 ордеров')
            else:
                print(label, 'контрольный ранее известный ордер виден:',
                      any(str(item.get('advOrderNo')) == known_no for item in known_orders))
    except (ValueError, InvalidOperation):
        print(label, 'MEXC вернул неполные данные; требуется ручная сверка')
    except Exception as exc:
        print(label, 'ошибка чтения:', type(exc).__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cycle_id')
    args = parser.parse_args()
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.cash2')), override=True)
    path = Path(os.environ['CYCLE_DB'])
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute('SELECT spec FROM cycles WHERE id=?', (args.cycle_id,)).fetchone()
        step = db.execute("SELECT status,result FROM steps WHERE cycle_id=? AND name='forward_create'",
                          (args.cycle_id,)).fetchone()
        attempted = db.execute("""SELECT time FROM events WHERE cycle_id=? AND step='forward_create'
            AND status='in_flight' ORDER BY id LIMIT 1""", (args.cycle_id,)).fetchone()
        control_row = db.execute("""SELECT s.result,e.time FROM cycles c
            JOIN steps s ON s.cycle_id=c.id AND s.name='forward_create' AND s.status='done'
            JOIN events e ON e.id=(SELECT MIN(id) FROM events WHERE cycle_id=c.id
                AND step='forward_create' AND status='in_flight')
            WHERE json_extract(c.spec,'$.p2_profile')=(SELECT json_extract(spec,'$.p2_profile')
                FROM cycles WHERE id=?) AND json_extract(s.result,'$.order_no') IS NOT NULL
            ORDER BY c.rowid DESC LIMIT 1""", (args.cycle_id,)).fetchone()
    if not row or not step or not attempted:
        raise SystemExit('Нет полного журнала попытки; история MEXC не запрошена')
    spec = json.loads(row[0])
    plan = json.loads(step[1])
    if (spec.get('scheduler_mode') != 'cash_volume' or step[0] != 'unknown'
            or not plan.get('adv_no') or not (plan.get('amount') or plan.get('tradable_quantity'))):
        raise SystemExit('Это не неопределённая попытка покупки налички; история MEXC не запрошена')
    started = datetime.fromisoformat(attempted[0])
    if started.tzinfo is None:
        raise SystemExit('У попытки нет часового пояса; история MEXC не запрошена')
    start_ms = int((started - timedelta(seconds=30)).timestamp() * 1000)
    end_ms = int((started + timedelta(minutes=10)).timestamp() * 1000)
    control = None
    if control_row:
        known_no = json.loads(control_row[0]).get('order_no')
        known_at = datetime.fromisoformat(control_row[1])
        if known_no and known_at.tzinfo is not None:
            control = (known_no,
                       int((known_at - timedelta(seconds=30)).timestamp() * 1000),
                       int((known_at + timedelta(minutes=10)).timestamp() * 1000))
    print('П2:', spec.get('p2_profile'), 'попытка UTC:', started.astimezone(timezone.utc).isoformat())
    for account in ('p2', 'p1'):
        settings = Settings.from_env(account, p2_profile=spec['p2_profile'] if account == 'p2' else None)
        client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                              settings.recv_window, settings.proxy_url)
        try:
            await inspect(client, account.upper(), plan, start_ms, end_ms, control)
        finally:
            await client.close()


if __name__ == '__main__':
    asyncio.run(main())
