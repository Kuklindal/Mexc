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


def shape(value):
    if isinstance(value, list):
        return ('list', len(value), list(value[0])[:12] if value and isinstance(value[0], dict) else [])
    if isinstance(value, dict):
        return ('object', len(value), list(value)[:12])
    return (type(value).__name__, bool(value))


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
    public_payment = ad.get('paymethodIds')
    public_payment_info = ad.get('paymentInfo') or []
    public_payment_ids = ([str(item.get('id')) for item in public_payment_info]
                          if isinstance(public_payment_info, list)
                          and all(isinstance(item, dict) and str(item.get('id', '')).isdigit()
                                  for item in public_payment_info) else [])
    print('Публичная связь оплаты: payMethod=paymentInfo.id',
          bool(public_payment_ids) and str(ad.get('payMethod')) == ','.join(public_payment_ids),
          flush=True)
    print('Публичный API: способы оплаты',
          len(public_payment) if isinstance(public_payment, list) else
          len(str(public_payment).split(',')) if public_payment else 0,
          'поля', [key for key in ad if 'paymethod' in key.lower() or 'payment' in key.lower()],
          flush=True)
    print('Публичные поля оплаты:', {key: shape(ad.get(key)) for key in
          ('payMethod', 'paymentInfo', 'paymethodIds', 'securityOrderPaymethodIds')}, flush=True)
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
                paymentKeys:Object.keys(d).filter(k=>/paymethod|payment/i.test(k)),
                paymentShapes:Object.fromEntries(['payMethod','paymentInfo','paymethodIds',
                    'securityOrderPaymethodIds'].map(k=>{const v=d[k];return [k,
                    Array.isArray(v)?['list',v.length,v[0]&&typeof v[0]==='object'?Object.keys(v[0]).slice(0,12):[]]:
                    v&&typeof v==='object'?['object',Object.keys(v).length,Object.keys(v).slice(0,12)]:
                    [typeof v,!!v]]})),
                paymentCount:Array.isArray(d.paymethodIds)?d.paymethodIds.length:
                    String(d.paymethodIds ?? '').split(',').filter(Boolean).length,
                paymentIdsValid:Array.isArray(d.paymentInfo) && d.paymentInfo.length > 0
                    && d.paymentInfo.every(x=>x && /^\d+$/.test(String(x.id)) && Number(x.id)>0),
                payMethodMatchesPaymentInfoIds:Array.isArray(d.paymentInfo) && d.paymentInfo.length > 0
                    && String(d.payMethod) === d.paymentInfo.map(x=>String(x.id)).join(','),
                payMethodMatchesPaymentInfoTypes:Array.isArray(d.paymentInfo) && d.paymentInfo.length > 0
                    && String(d.payMethod) === d.paymentInfo.map(x=>String(x.payMethod)).join(','),
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
                if value.get('state') != 1:
                    mismatches.append('статус=' + repr(value.get('state')))
                if value.get('tradeType') != 1:
                    mismatches.append('сторона=' + repr(value.get('tradeType')))
                if value.get('priceType') != 0:
                    mismatches.append('тип цены=' + repr(value.get('priceType')))
                if value.get('coinName') != 'USDT':
                    mismatches.append('токен=' + repr(value.get('coinName')))
                if value.get('currency') != ad.get('fiatUnit'):
                    mismatches.append('валюта=' + repr(value.get('currency')))
                if not value.get('paymentCount'):
                    mismatches.append('нет способов оплаты')
                print('Вкладка', host, 'API', value.get('code'), 'лимиты',
                      value.get('minimum'), '–', value.get('maximum'),
                      'несовпадения:', ', '.join(mismatches) or 'нет',
                      'поля оплаты:', value.get('paymentKeys'),
                      'формы:', value.get('paymentShapes'),
                      'связь:', {key: value.get(key) for key in
                                 ('paymentIdsValid', 'payMethodMatchesPaymentInfoIds',
                                  'payMethodMatchesPaymentInfoTypes')}, flush=True)
            except Exception as exc:
                print('Вкладка', host, 'ошибка', type(exc).__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
