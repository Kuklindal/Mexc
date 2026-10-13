"""Read-only network checks for both makers in a pending Eflp transition."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower, AdsPowerError  # noqa: E402
from config import ENV_FILE  # noqa: E402


async def browser_probe(browser: AdsPower, host: str) -> str:
    async with browser.connection() as call:
        target_id = (await call('Target.createTarget', {
            'url': 'about:blank', 'background': True}, timeout=15)).get('targetId')
        if not target_id:
            return 'временная вкладка не открылась'
        try:
            session = (await call('Target.attachToTarget', {
                'targetId': target_id, 'flatten': True}, timeout=12)).get('sessionId')
            if not session:
                return 'CDP не подключился'
            expression = (
                '(async()=>{const c=new AbortController();const t=setTimeout(()=>c.abort(),8000);'
                'try{await fetch(' + json.dumps(f'https://{host}/favicon.ico') + ','
                "{mode:'no-cors',cache:'no-store',signal:c.signal});return 'сеть отвечает'}"
                "catch(e){return e.name==='AbortError'?'таймаут':'сбой сети'}"
                'finally{clearTimeout(t)}})()'
            )
            result = await call('Runtime.evaluate', {
                'expression': expression, 'awaitPromise': True,
                'returnByValue': True}, session, timeout=15)
            return str(result.get('result', {}).get('value') or 'JS не ответил')
        finally:
            try:
                await call('Target.closeTarget', {'targetId': target_id}, timeout=8)
            except AdsPowerError:
                pass


async def main() -> None:
    load_dotenv(ENV_FILE, override=True)
    path = Path(os.getenv('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        state = json.loads(db.execute(
            "SELECT value FROM meta WHERE key='rollover_scheduler_v1'").fetchone()[0])
    pending = state.get('terminal_pending') or {}
    names = [state['p1_profile']]
    if pending.get('to') and pending['to'] not in names:
        names.append(pending['to'])
    for name in names:
        prefix = 'MEXC_P1' if name == 'p1' else 'MEXC_' + name.upper()
        proxy = os.getenv(prefix + '_PROXY_URL') or None
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=12,
                                         trust_env=False) as client:
                response = await client.get('https://api.mexc.com/api/v3/ping')
            api = f'HTTP {response.status_code}'
        except (httpx.HTTPError, ImportError, ValueError) as exc:
            api = type(exc).__name__
        print(name, 'API-прокси:', api, flush=True)
        browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                           os.getenv('ADSPOWER_API_KEY', ''),
                           os.getenv(prefix + '_ADSPOWER_PROFILE_ID', ''))
        for host in ('www.mexc.com', 'www.mexc.co'):
            try:
                result = await browser_probe(browser, host)
            except AdsPowerError as exc:
                result = type(exc).__name__
            print(name, host, result, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
