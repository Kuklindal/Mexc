"""Read the active maker's MEXC tabs and ad visibility without changing an order."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower  # noqa: E402
from config import ENV_FILE  # noqa: E402


async def main() -> None:
    path = Path(os.getenv('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()
    if not row:
        raise RuntimeError('В выбранной базе нет состояния серии')
    state = json.loads(row[0])
    next_buy = '--next-buy' in sys.argv
    pending = state.get('terminal_pending') or {}
    name = pending.get('to') if next_buy else state['p1_profile']
    if not name:
        raise RuntimeError('В состоянии серии нет следующего П1')
    prefix = 'MEXC_P1' if name == 'p1' else 'MEXC_' + name.upper()
    profile_key = ('ADSPOWER_P1_PROFILE_ID' if name == 'p1'
                   else prefix + '_ADSPOWER_PROFILE_ID')
    profile_id = os.getenv(profile_key, '').strip()
    side = 'BUY' if next_buy else 'SELL'
    adv_no = os.getenv(prefix + f'_{side}_ADV_NO', '').strip()
    if not profile_id or not adv_no:
        raise RuntimeError(f'Проверьте {profile_key} и {prefix}_{side}_ADV_NO')
    print('Конфигурация:', ENV_FILE.name, 'П1:', name, 'AdsPower ID:', profile_id,
          'объявление:', side, 'этап:', state.get('status'), flush=True)

    browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''), profile_id)
    expression = (
        '(async()=>{const out={host:location.hostname,protocol:location.protocol,'
        'ready:document.readyState};const adv=' + json.dumps(adv_no) + ';'
        "if(location.protocol!=='https:'||!['mexc.com','www.mexc.com','mexc.co',"
        "'www.mexc.co','mexc.io','www.mexc.io'].includes(location.hostname))return out;"
        "try{const r=await fetch('/api/platform/p2p/api/merchant/order/'+adv,"
        "{credentials:'same-origin',cache:'no-store'});"
        "out.http=r.status;out.contentType=(r.headers.get('content-type')||'').split(';')[0];"
        "let b;try{b=await r.json()}catch(_){out.error='non_json';return out}"
        'out.code=b.code;out.ownsAd=b.data?.id===adv}'
        "catch(e){out.error='fetch_'+(e?.name||'Error')}"
        "if(out.error){try{const p=await fetch('/favicon.ico',{cache:'no-store'});"
        "out.siteHttp=p.status}catch(e){out.siteError='fetch_'+(e?.name||'Error')}}"
        "return out})()"
    )
    async with browser.connection() as call:
        targets = (await call('Target.getTargets', timeout=15)).get('targetInfos', [])
        pages = [target for target in targets if target.get('type') == 'page'
                 and 'mexc' in (urlsplit(target.get('url', '')).hostname or '')]
        created_target_ids = set()
        for flag, host in (('--open-com', 'www.mexc.com'), ('--open-co', 'www.mexc.co')):
            if flag not in sys.argv or any(
                    urlsplit(target.get('url', '')).hostname == host for target in pages):
                continue
            url = f'https://{host}/ru-RU/buy-crypto/create-advertising'
            created = await call('Target.createTarget', {
                'url': url, 'background': True}, timeout=30)
            target_id = created.get('targetId')
            if not target_id:
                raise RuntimeError(f'AdsPower не открыл вкладку {host}')
            created_target_ids.add(target_id)
            pages.append({'type': 'page', 'targetId': target_id, 'url': url})
        if '--open-api' in sys.argv:
            for host in ('www.mexc.com', 'www.mexc.co'):
                url = f'https://{host}/api/platform/p2p/api/merchant/order'
                if any(target.get('url') == url for target in pages):
                    continue
                created = await call('Target.createTarget', {
                    'url': url, 'background': True}, timeout=30)
                target_id = created.get('targetId')
                if not target_id:
                    raise RuntimeError(f'AdsPower не открыл страницу API {host}')
                created_target_ids.add(target_id)
                pages.append({'type': 'page', 'targetId': target_id, 'url': url})
        print('Вкладок MEXC:', len(pages), flush=True)
        for target in pages:
            listed_url = urlsplit(target.get('url', ''))
            listed_host = (listed_url.hostname or '—') + (
                ' API' if listed_url.path.startswith('/api/') else '')
            try:
                attached = await call('Target.attachToTarget', {
                    'targetId': target['targetId'], 'flatten': True}, timeout=12)
                value = None
                for attempt in range(10 if target.get('targetId') in created_target_ids else 1):
                    if attempt:
                        await asyncio.sleep(2)
                    result = await call('Runtime.evaluate', {
                        'expression': expression, 'awaitPromise': True,
                        'returnByValue': True}, attached['sessionId'], timeout=15)
                    value = result.get('result', {}).get('value')
                    if isinstance(value, dict) and value.get('ready') == 'complete' and 'code' in value:
                        break
                if result.get('exceptionDetails') or not isinstance(value, dict):
                    print('Вкладка:', listed_host, 'JS не ответил', flush=True)
                else:
                    print('Вкладка:', listed_host, '→', value, flush=True)
            except Exception as exc:
                print('Вкладка:', listed_host, 'ошибка:', type(exc).__name__, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
