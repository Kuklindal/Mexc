"""Compare the public and browser SELL snapshots used before changing Eflp limits."""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from eflp_ads import maker_browser  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402
from trade_profiles import profile_from_env  # noqa: E402


def same(left, right) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError):
        return False


async def main() -> None:
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.eflp-unique2')),
                override=True)
    path = Path(os.environ['CYCLE_DB'])
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get('mode') != 'eflp_unique' or state.get('active_cycle'):
        raise SystemExit('Нет остановленной серии Eflp без активного цикла')
    profile = profile_from_env(state['p1_profile'], os.environ)
    settings = Settings.from_env('p1', p1_profile=profile.key)
    client = MexcP2PClient(settings.api_key, settings.secret_key,
                          settings.base_url, settings.recv_window, settings.proxy_url)
    try:
        ad = await client.get_ad(profile.sell_adv_no)
    finally:
        await client.close()
    print('П1', profile.key, 'SELL API:', ad.get('advStatus'),
          ad.get('availableQuantity'), 'USDT, цена', ad.get('price'),
          'лимиты', ad.get('minSingleTransAmount'), '–', ad.get('maxSingleTransAmount'), flush=True)
    browser = maker_browser(profile, os.environ)
    expression = """(async (advNo) => {
        try {
            const r = await fetch('/api/platform/p2p/api/merchant/order/' + advNo,
                {credentials:'same-origin',cache:'no-store'});
            const b = await r.json(); const d = b.data || {};
            return {host:location.hostname,http:r.status,code:b.code,ownsAd:d.id===advNo,
                coinName:d.coinName,currency:d.currency,tradeType:d.tradeType,state:d.state,
                available:d.availableQuantity,frozen:d.frozenQuantity ?? 0,price:d.price,
                minimum:d.minTradeLimit,maximum:d.maxTradeLimit,
                paymentCount:Array.isArray(d.paymethodIds)?d.paymethodIds.length:
                    String(d.paymethodIds ?? '').split(',').filter(Boolean).length,
                coinIdPresent:d.coinId!=null,priceType:d.priceType,
                expirationPresent:d.expirationTime!=null,kycPresent:d.kycLevel!=null,
                displayPresent:d.display!=null,adsTypePresent:d.adsType!=null,
                merchantTradePresent:d.merchantTradeEnable!=null,
                onlyKybPresent:d.onlyTradeKybUser!=null};
        } catch (_) { return {host:location.hostname,error:'read_failed'}; }
    })(""" + json.dumps(profile.sell_adv_no) + ")"
    async with browser.connection() as call:
        targets = [item for item in (await call('Target.getTargets')).get('targetInfos', [])
                   if item.get('type') == 'page' and (urlsplit(item.get('url', '')).hostname or '')
                   in {'www.mexc.com', 'mexc.com', 'www.mexc.co', 'mexc.co', 'www.mexc.io', 'mexc.io'}]
        if not targets:
            print('Вкладок MEXC нет')
        for target in targets:
            host = urlsplit(target.get('url', '')).hostname
            try:
                session = (await call('Target.attachToTarget', {
                    'targetId': target['targetId'], 'flatten': True}))['sessionId']
                result = await call('Runtime.evaluate', {
                    'expression': expression, 'awaitPromise': True, 'returnByValue': True},
                    session, timeout=15)
                value = result.get('result', {}).get('value') or {}
                if not value.get('ownsAd'):
                    print('Вкладка', host, 'API', value.get('code'), 'объявление не видно', flush=True)
                    continue
                mismatches = []
                for field, public, internal in (
                        ('остаток', ad.get('availableQuantity'), value.get('available')),
                        ('заморожено', ad.get('frozenQuantity') or 0, value.get('frozen')),
                        ('цена', ad.get('price'), value.get('price')),
                        ('минимум', ad.get('minSingleTransAmount'), value.get('minimum')),
                        ('максимум', ad.get('maxSingleTransAmount'), value.get('maximum'))):
                    if not same(public, internal):
                        mismatches.append(field)
                required = ('coinIdPresent', 'expirationPresent', 'kycPresent',
                            'displayPresent', 'adsTypePresent', 'merchantTradePresent',
                            'onlyKybPresent')
                mismatches.extend(field for field in required if not value.get(field))
                if (value.get('state') != 1 or value.get('tradeType') != 1
                        or value.get('priceType') != 0 or value.get('coinName') != 'USDT'
                        or value.get('currency') != ad.get('fiatUnit')
                        or not value.get('paymentCount')):
                    mismatches.append('тип/статус/реквизит/валюта')
                print('Вкладка', host, 'API', value.get('code'), 'лимиты',
                      value.get('minimum'), '–', value.get('maximum'),
                      'несовпадения:', ', '.join(mismatches) or 'нет', flush=True)
            except Exception as exc:
                print('Вкладка', host, 'ошибка', type(exc).__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
