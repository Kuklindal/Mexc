"""Read both SELL snapshots for a paused reverse_replenish step; never change an ad."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from eflp_ads import maker_browser  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402
from trade_profiles import profile_from_env  # noqa: E402


def number(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('Использование: inspect_refill_mismatch.py CYCLE_ID')
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.eflp')), override=True)
    path = Path(os.environ.get('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        cycle = db.execute('SELECT status,spec FROM cycles WHERE id=?', (sys.argv[1],)).fetchone()
        step = db.execute("SELECT status,result FROM steps WHERE cycle_id=? AND name='reverse_replenish'",
                          (sys.argv[1],)).fetchone()
    if not cycle or not step:
        raise SystemExit('Цикл или шаг reverse_replenish не найден')
    spec = json.loads(cycle[1])
    plan = json.loads(step[1]) if step[1] else {}
    if spec.get('scheduler_mode') not in {'eflp_volume', 'eflp_unique'}:
        raise SystemExit('Это не цикл Eflp')
    maker_key = spec.get('reverse_p1_profile') or spec.get('p1_profile')
    profile = profile_from_env(maker_key, os.environ)
    adv_no = plan.get('adv_no')
    if not adv_no:
        raise SystemExit('В сохранённом шаге нет номера SELL-объявления')
    print('Цикл', sys.argv[1], cycle[0], 'шаг', step[0], 'П1', maker_key,
          'профиль AdsPower', profile.adspower_profile_id, flush=True)
    print('SELL в плане совпадает с конфигурацией:', adv_no == profile.sell_adv_no,
          'валюта плана:', plan.get('fiat') or spec.get('reverse_fiat', spec.get('fiat')), flush=True)
    print('План остатка:', plan.get('before_available'), '→', plan.get('target_available'),
          'USDT; пополнить', plan.get('quantity'), flush=True)
    settings = Settings.from_env('p1', p1_profile=maker_key)
    client = MexcP2PClient(settings.api_key, settings.secret_key,
                          settings.base_url, settings.recv_window, settings.proxy_url)
    try:
        api = await client.get_ad(adv_no)
    finally:
        await client.close()
    print('API:', api.get('advStatus'), api.get('side'), api.get('fiatUnit'),
          'доступно', api.get('availableQuantity'), 'заморожено', api.get('frozenQuantity'), flush=True)
    browser = maker_browser(profile, os.environ)
    try:
        viewed = await browser.ad_details(adv_no)
    except Exception as exc:
        print('Браузер:', type(exc).__name__, str(exc)[:180], flush=True)
        return
    print('Браузер: ID совпадает', viewed.get('id') == adv_no,
          'токен', viewed.get('coinName'), 'сторона', viewed.get('tradeType'),
          'валюта', viewed.get('currency'), 'доступно', viewed.get('availableQuantity'),
          'заморожено', viewed.get('frozenQuantity'), flush=True)
    api_available = number(api.get('availableQuantity'))
    browser_available = number(viewed.get('availableQuantity'))
    api_frozen = number(api.get('frozenQuantity'))
    browser_frozen = number(viewed.get('frozenQuantity'))
    if api_frozen is not None and browser_frozen is not None:
        balances_match = (api_available is not None and browser_available is not None
                          and api_available + api_frozen == browser_available + browser_frozen)
    else:
        balances_match = api_available is not None and api_available == browser_available
    checks = {
        'ID': viewed.get('id') == adv_no and api.get('advNo') == adv_no,
        'токен': viewed.get('coinName') == 'USDT',
        'сторона': viewed.get('tradeType') == 1 and api.get('side') == 'SELL',
        'валюта': viewed.get('currency') == (plan.get('fiat') or spec.get('reverse_fiat', spec.get('fiat'))),
        'остаток': balances_match,
    }
    print('Расхождения:', ', '.join(key for key, matched in checks.items() if not matched) or 'нет', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
