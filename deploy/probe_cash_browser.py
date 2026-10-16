"""Measure cash2 P1's CDP and authenticated MEXC read without changing orders."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower


async def main() -> None:
    load_dotenv(ROOT / '.env.cash2', override=True)
    browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''),
                       os.environ['ADSPOWER_P1_PROFILE_ID'])
    adv_no = os.environ['MEXC_P1_SELL_ADV_NO']
    async with browser.connection() as call:
        start = time.monotonic()
        targets = (await call('Target.getTargets', timeout=15)).get('targetInfos', [])
        print('AdsPower список вкладок: %.1f сек' % (time.monotonic() - start), flush=True)
        merchant = next((target for target in targets
                         if target.get('type') == 'page'
                         and urlsplit(target.get('url', '')).hostname == 'www.mexc.com'
                         and urlsplit(target['url']).path.endswith('/buy-crypto/control')), None)
        if not merchant:
            print('Портал мерчанта .com: вкладка не найдена', flush=True)
            return
        session = (await call('Target.attachToTarget', {
            'targetId': merchant['targetId'], 'flatten': True}, timeout=15))['sessionId']
        for index in range(1, 5):
            expression = ('1+1' if index == 1 else
                          "(async()=>{try{const r=await fetch('/api/platform/p2p/api/merchant/order/'+"
                          + json.dumps(adv_no) + ",{credentials:'same-origin',cache:'no-store'});"
                          "const j=await r.json();return {http:r.status,code:j.code,owns:j.data?.id==="
                          + json.dumps(adv_no) + "}}catch(_){return {error:'read_failed'}}})()")
            start = time.monotonic()
            try:
                response = await call('Runtime.evaluate', {
                    'expression': expression, 'awaitPromise': index != 1,
                    'returnByValue': True}, session, timeout=20)
                result = response.get('result', {}).get('value')
                if index == 1:
                    print('AdsPower простой вызов: %.1f сек' % (time.monotonic() - start), flush=True)
                else:
                    summary = ({key: result.get(key) for key in ('http', 'code', 'owns', 'error')}
                               if isinstance(result, dict) else {'error': 'no_result'})
                    print('MEXC в браузере', index - 1, 'сек', round(time.monotonic() - start, 1),
                          summary, flush=True)
            except Exception as exc:
                print(('AdsPower простой вызов' if index == 1 else f'MEXC в браузере {index - 1}'),
                      type(exc).__name__, 'сек', round(time.monotonic() - start, 1), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
