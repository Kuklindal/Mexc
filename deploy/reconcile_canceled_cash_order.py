"""Record a cash order cancelled on both accounts, then release its journal timer."""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from journal import Journal  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402
from rollover import clear_cooldown, load_state  # noqa: E402


def stopped_service() -> bool:
    result = subprocess.run(['systemctl', '--user', 'is-active',
                             'mexc-parallel@cash2.service'], capture_output=True,
                            text=True, timeout=10)
    return result.stdout.strip() in {'inactive', 'failed'}


async def order_detail(account: str, profile: str, order_no: str) -> dict:
    settings = Settings.from_env(account, p2_profile=profile if account == 'p2' else None)
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                          settings.recv_window, settings.proxy_url)
    try:
        return await client.get_order_detail(order_no)
    finally:
        await client.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('order_no')
    parser.add_argument('p2_profile')
    args = parser.parse_args()
    if not stopped_service():
        raise SystemExit('cash2 ещё работает; журнал не изменён')
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.cash2')), override=True)
    path = Path(os.environ['CYCLE_DB'])
    if not path.is_absolute():
        path = ROOT / path
    journal = Journal(path)
    try:
        state = load_state(journal)
        if not state or state.get('mode') != 'cash_volume' or state.get('status') != 'paused':
            raise SystemExit('cash2 не на паузе в режиме объёма; журнал не изменён')
        if state.get('active_cycle') or state.get('pending_return'):
            raise SystemExit('Есть активный цикл или возврат; журнал не изменён')
        rows = journal.db.execute("""SELECT c.id,c.status,c.spec,s.result
            FROM cycles c JOIN steps s ON s.cycle_id=c.id AND s.name='forward_create'
            WHERE s.status='done' AND json_extract(s.result,'$.order_no')=?""",
            (args.order_no,)).fetchall()
        matches = [row for row in rows if json.loads(row['spec']).get('p2_profile') == args.p2_profile]
        if len(matches) != 1 or matches[0]['status'] != 'abandoned':
            raise SystemExit('Не найден один сброшенный цикл этого П2 с данным ордером; журнал не изменён')
        cycle_id = matches[0]['id']
        if journal.step(cycle_id, 'forward_complete') and journal.step(cycle_id, 'forward_complete')['status'] == 'done':
            raise SystemExit('Покупка уже подтверждена в журнале; таймер не снят')
        if journal.step(cycle_id, 'forward_cancel_confirmed'):
            raise SystemExit('Отмена уже записана в журнале; повторная запись не нужна')

        try:
            p1 = await order_detail('p1', args.p2_profile, args.order_no)
            p2 = await order_detail('p2', args.p2_profile, args.order_no)
        except Exception as exc:
            raise SystemExit(f'Проверка MEXC не удалась ({type(exc).__name__}); журнал не изменён') from None
        if p1.get('state') != 'CANCEL' or p2.get('state') != 'CANCEL':
            raise SystemExit('Отмена не подтверждена обоими аккаунтами; журнал не изменён')
        if any(detail.get('advOrderNo') not in (None, args.order_no) for detail in (p1, p2)):
            raise SystemExit('Номер ордера в ответе не совпал; журнал не изменён')
        try:
            quantity_p1 = Decimal(str(p1.get('tradableQuantity')))
            quantity_p2 = Decimal(str(p2.get('tradableQuantity')))
        except (InvalidOperation, TypeError):
            raise SystemExit('MEXC не вернул количество USDT; журнал не изменён') from None
        if not quantity_p1.is_finite() or quantity_p1 <= 0 or quantity_p1 != quantity_p2:
            raise SystemExit('Количество USDT у аккаунтов не совпало; журнал не изменён')

        journal.transition(cycle_id, 'forward_cancel_confirmed', 'system', 'done',
                           'MEXC P1 и P2 подтвердили отмену ордера',
                           result={'order_no': args.order_no, 'p1_state': 'CANCEL',
                                   'p2_state': 'CANCEL'},
                           context={'order_no': args.order_no,
                                    'quantity': str(quantity_p1)})
        clear_cooldown(journal, args.p2_profile)
        print('Отмена подтверждена обоими аккаунтами; таймер П2 снят в журнале.', flush=True)
    finally:
        journal.close()


if __name__ == '__main__':
    asyncio.run(main())
