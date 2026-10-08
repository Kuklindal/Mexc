"""Attach to P1's AdsPower profile; only approve the explicitly confirmed order."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import traceback
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import httpx
import websockets


class AdsPowerError(RuntimeError):
    pass


class AdsPowerTimeout(AdsPowerError):
    pass


class AdsPowerUnavailable(AdsPowerError):
    """Temporary failure reaching the local AdsPower API before a browser command."""
    pass


class AdsPowerClickUnknown(AdsPowerError):
    """The browser may have processed a click before its CDP reply was lost."""
    pass


def _safe_error_location(exc: Exception) -> str:
    """Identify a failing code line without logging CDP payloads or signed URLs."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno}"


# The site's quantity endpoint changes only the available quantity. It does not
# resave trade requirements like the public save_or_update endpoint does.
AD_QUANTITY = r"""async (advNo, plan) => {
    if (location.protocol !== 'https:' || !['mexc.com','www.mexc.com'].includes(location.hostname))
        return {error:'origin'};
    const root = '/api/platform/p2p/api/merchant/order';
    const read = async () => {
        const response = await fetch(root + '/' + advNo, {method:'GET',credentials:'same-origin',cache:'no-store'});
        const body = await response.json(), d = body.data;
        if (!response.ok || body.code !== 0 || !d || d.id !== advNo) throw new Error('ad_read');
        return {id:d.id, availableQuantity:d.availableQuantity, frozenQuantity:d.frozenQuantity,
            overVerify:d.overVerify ?? null,
            coinName:d.coinName, currency:d.currency, tradeType:d.tradeType};
    };
    const verification = value => {
        const data = typeof value === 'string' ? JSON.parse(value) : value;
        if (!data || !Array.isArray(data.types)) return JSON.stringify(data);
        const result = {types:[...data.types].sort((a,b)=>a-b)};
        if (data.types.includes(6)) result.otherText = data.otherText;
        return JSON.stringify(result);
    };
    const before = await read();
    if (!plan) return before;
    const total = d => Number(d.availableQuantity) + Number(d.frozenQuantity);
    const sameTotal = (d, expected) => Number.isFinite(total(d)) && Number.isFinite(Number(expected))
        && Math.abs(total(d) - Number(expected)) <= 1e-8;
    if (before.coinName !== 'USDT' || before.currency !== plan.fiat
            || before.tradeType !== (plan.side === 'BUY' ? 0 : 1)
            || (plan.target_total !== undefined ? !sameTotal(before, plan.before_total)
                : Number(before.availableQuantity) !== Number(plan.before_available))
            || verification(before.overVerify) !== verification(plan.over_verify)) return {error:'ad_changed'};
    const response = await fetch(root + '/quantity', {method:'POST',credentials:'same-origin',
        body:new URLSearchParams({id:advNo,quantity:plan.quantity})});
    const body = await response.json();
    if (!response.ok || body.code !== 0) return {error:'quantity_rejected',code:body.code,http:response.status};
    const after = await read();
    if ((plan.target_total !== undefined ? !sameTotal(after, plan.target_total)
            : Number(after.availableQuantity) !== Number(plan.target_available))
            || verification(after.overVerify) !== verification(before.overVerify)) return {error:'result_mismatch'};
    return after;
}"""


# Inspected on MEXC's merchant order dialog. Do not search/click by text globally.
ORDER_VIEW = r"""(orderNo, click) => {
    if (location.protocol !== 'https:' || !['mexc.com', 'www.mexc.com'].includes(location.hostname))
        return {state: 'blocked'};
    const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const urlIds = new URLSearchParams(location.search).getAll('id');
    const orderPage = location.pathname.endsWith('/buy-crypto/order-processing')
        && urlIds.length === 1 && urlIds[0] === orderNo;
    if (!orderPage && !location.pathname.endsWith('/buy-crypto/control')) return {state: 'blocked'};
    const dialogs = orderPage ? [document.body] : Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible);
    const matches = dialogs.filter(d => {
        if (!d) return false;
        const ids = [...new Set(d.innerText.match(/d\d{15,25}/g) || [])];
        // The standalone order page omits its number from rendered text.
        // Bind it to the exact URL and its order heading; reject conflicting IDs.
        if (orderPage && ids.length === 0) {
            const headings = Array.from(d.querySelectorAll('h1,h2,h3')).filter(visible);
            return headings.some(h => ['Ожидание проверки', 'Ожидание оплаты'].includes(h.innerText.trim()));
        }
        return ids.length === 1 && ids[0] === orderNo;
    });
    if (matches.length !== 1) return {state: matches.length ? 'ambiguous' : 'missing'};
    const dialog = matches[0];
    const buttons = Array.from(dialog.querySelectorAll('button')).filter(visible)
        .filter(b => b.innerText.trim() === 'Проверка пройдена');
    if (buttons.length > 1) return {state: 'ambiguous'};
    const waiting = dialog.innerText.includes('Ожидание проверки');
    if (!buttons.length && !waiting && dialog.innerText.includes('Ожидание оплаты'))
        return {state: 'passed'};
    if (!waiting || buttons.length !== 1 || buttons[0].disabled || buttons[0].getAttribute('aria-disabled') === 'true')
        return {state: 'blocked'};
    if (click) buttons[0].click();
    return {state: click ? 'clicked' : 'ready'};
}"""


# The authenticated MEXC payment page uses this read-only endpoint to list
# account-specific IDs. Return only the IDs matching the configured type;
# never export card numbers, bank details, cookies or the whole response.
PAYMENT_ACCOUNT_VIEW = r"""async (method, fiat) => {
    if (location.protocol !== 'https:' ||
            !['mexc.com','www.mexc.com','mexc.co','www.mexc.co','mexc.io','www.mexc.io'].includes(location.hostname))
        return {error:'origin'};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
        const response = await fetch('/api/payment/user', {
            method:'GET',credentials:'same-origin',cache:'no-store',signal:controller.signal});
        if (!response.ok) return {error:'http'};
        const body = await response.json();
        if (body.code !== 0) return {error:'api'};
        const data = body.data;
        const entries = Array.isArray(data) ? data :
            Array.isArray(data?.list) ? data.list :
            Array.isArray(data?.rows) ? data.rows :
            Array.isArray(data?.records) ? data.records :
            Array.isArray(data?.data) ? data.data :
            data && data.id != null && data.payMethod != null ? [data] : null;
        if (!entries) return {error:'format'};
        const matches = entries.filter(item => {
            if (!item || String(item.payMethod) !== String(method) || item.enabled === false || item.disabled === true)
                return false;
            const currency = item.fiatUnit ?? item.fiat ?? item.currency;
            return !currency || String(currency).toUpperCase() === fiat;
        }).map(item => String(item.id));
        return {ids:matches};
    } catch (_) {
        return {error:'read'};
    } finally {
        clearTimeout(timer);
    }
}"""


def local_url(value: str, schemes: set[str]) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise AdsPowerError("AdsPower должен использовать локальный адрес 127.0.0.1 или localhost")
    return value


class AdsPower:
    def __init__(self, base_url: str, api_key: str, profile_id: str):
        self.base_url = local_url(base_url.rstrip('/'), {"http", "https"})
        self.api_key, self.profile_id = api_key.strip(), profile_id.strip()
        self.command_timeout = 10

    @classmethod
    def from_env(cls):
        return cls(os.getenv("ADSPOWER_BASE_URL", "http://127.0.0.1:50325"),
                   os.getenv("ADSPOWER_API_KEY", ""), os.getenv("ADSPOWER_P1_PROFILE_ID", ""))

    async def ensure_started(self) -> None:
        """Start only this AdsPower profile, leaving other active roles open."""
        if not self.api_key or not self.profile_id:
            raise AdsPowerError('Для запуска профиля нужны ключ AdsPower и его ID')
        headers = {'Authorization': 'Bearer ' + self.api_key}
        async with httpx.AsyncClient(timeout=max(15, self.command_timeout), trust_env=False) as client:
            started = False
            for _ in range(24):
                try:
                    response = await client.get(self.base_url + '/api/v1/browser/active',
                                                params={'user_id': self.profile_id}, headers=headers)
                except httpx.RequestError as exc:
                    raise AdsPowerUnavailable(f'AdsPower Local API недоступен ({type(exc).__name__})') from None
                payload = response.json()
                if response.status_code != 200:
                    raise AdsPowerError(f'AdsPower HTTP {response.status_code}: проверьте Local API и ключ')
                if payload.get('code') == -1:
                    # -1 is a generic failure; a concurrent profile operation may
                    # be temporary. This read-only preflight is safe to retry.
                    await asyncio.sleep(5)
                    continue
                if payload.get('code') != 0:
                    raise AdsPowerError('AdsPower отклонил проверку профиля')
                if payload.get('data', {}).get('status') == 'Active':
                    await self.endpoint()
                    return
                if not started:
                    response = await client.get(self.base_url + '/api/v1/browser/start',
                                                params={'user_id': self.profile_id, 'headless': 1}, headers=headers)
                    payload = response.json()
                    if response.status_code != 200 or payload.get('code') != 0:
                        raise AdsPowerError('AdsPower не смог открыть профиль')
                    started = True
                await asyncio.sleep(5)
        raise AdsPowerUnavailable('AdsPower не подтвердил запуск профиля за 120 секунд')

    async def payment_account_by_method(self, method_id: int, fiat: str) -> int:
        """Read this account's own payment ID by type, without exporting bank details."""
        if type(method_id) is not int or method_id <= 0 or not re.fullmatch(r'[A-Z]{3}', fiat):
            raise ValueError('Для поиска реквизитов нужны payMethod и валюта')
        if not self.api_key or not self.profile_id:
            raise AdsPowerError('Для чтения реквизитов П2 нужны ADSPOWER_API_KEY и его профиль AdsPower')
        try:
            async with httpx.AsyncClient(timeout=max(15, self.command_timeout), trust_env=False) as client:
                response = await client.get(self.base_url + '/api/v1/browser/active',
                    params={'user_id': self.profile_id},
                    headers={'Authorization': 'Bearer ' + self.api_key})
                payload = response.json()
        except (httpx.RequestError, ValueError):
            raise AdsPowerUnavailable('Не удалось проверить состояние профиля П2 AdsPower') from None
        if response.status_code != 200 or not isinstance(payload, dict) or payload.get('code') != 0:
            raise AdsPowerError('AdsPower не подтвердил состояние профиля П2')
        data = payload.get('data')
        was_active = isinstance(data, dict) and data.get('status') == 'Active'
        if not was_active:
            await self.ensure_started()
        try:
            return await self._read_payment_account(method_id, fiat)
        finally:
            # Profiles already open before this read belong to the operator.
            if not was_active:
                await self.stop_profile()

    async def _read_payment_account(self, method_id: int, fiat: str) -> int:
        created_id = None
        async with self.connection() as call:
            try:
                for _ in range(10):
                    targets = (await call('Target.getTargets', timeout=20)).get('targetInfos', [])
                    pages = [item for item in targets if item.get('type') == 'page'
                             and urlsplit(item.get('url', '')).scheme == 'https'
                             and urlsplit(item.get('url', '')).hostname in {
                                 'mexc.com', 'www.mexc.com', 'mexc.co', 'www.mexc.co',
                                 'mexc.io', 'www.mexc.io'}]
                    pages.sort(key=lambda item: 'payment' not in urlsplit(item['url']).path)
                    if not pages and not created_id:
                        created = await call('Target.createTarget', {
                            'url': 'https://www.mexc.co/buy-crypto/payment', 'background': True}, timeout=30)
                        created_id = created.get('targetId')
                        if not created_id:
                            raise AdsPowerError('AdsPower не открыл страницу способов оплаты П2')
                    found_ids = set()
                    saw_list = False
                    for page in pages:
                        try:
                            attached = await call('Target.attachToTarget', {
                                'targetId': page['targetId'], 'flatten': True}, timeout=20)
                        except AdsPowerError:
                            continue
                        session = attached.get('sessionId')
                        if not session:
                            continue
                        try:
                            result = await call('Runtime.evaluate', {
                                'expression': f'({PAYMENT_ACCOUNT_VIEW})({method_id}, {json.dumps(fiat)})',
                                'awaitPromise': True, 'returnByValue': True}, session, timeout=20)
                        except AdsPowerTimeout:
                            continue
                        value = result.get('result', {}).get('value')
                        if 'exceptionDetails' in result or not isinstance(value, dict):
                            continue
                        ids = value.get('ids')
                        if isinstance(ids, list):
                            saw_list = True
                            if len(ids) > 1 or (ids and (not str(ids[0]).isascii()
                                    or not str(ids[0]).isdecimal() or int(ids[0]) <= 0)):
                                raise AdsPowerError(f'В профиле П2 найдено {len(ids)} реквизитов с payMethod {method_id} для {fiat}; нужен ровно один')
                            if ids:
                                found_ids.add(int(ids[0]))
                            continue
                        if value.get('error') in {'api', 'format'}:
                            continue
                    if len(found_ids) == 1:
                        return found_ids.pop()
                    if len(found_ids) > 1:
                        raise AdsPowerError('Вкладки профиля П2 вернули разные реквизиты; проверьте вход в MEXC')
                    if saw_list:
                        raise AdsPowerError(f'В профиле П2 не найден payMethod {method_id} для {fiat}')
                    await asyncio.sleep(3)
                raise AdsPowerUnavailable('MEXC не ответил на чтение способов оплаты П2 через AdsPower')
            finally:
                if created_id:
                    try:
                        await call('Target.closeTarget', {'targetId': created_id}, timeout=10)
                    except AdsPowerError:
                        pass

    async def ensure_mexc_page(self) -> None:
        """Require a rendered MEXC page, not merely a matching tab URL."""
        async with self.connection() as call:
            targets = (await call('Target.getTargets')).get('targetInfos', [])
            def is_mexc(target):
                url = urlsplit(target.get('url', ''))
                return (target.get('type') == 'page' and url.scheme == 'https'
                        and url.hostname in {'mexc.com', 'www.mexc.com'})
            pages = [target for target in targets if is_mexc(target)]
            if not pages:
                created = await call('Target.createTarget', {
                    'url': 'https://www.mexc.com/ru-RU/buy-crypto/', 'background': False}, timeout=30)
                target_id = created.get('targetId')
                if not target_id:
                    raise AdsPowerError('AdsPower не подтвердил создание вкладки MEXC')
            sessions = {}
            for _ in range(15):
                if not pages:
                    targets = (await call('Target.getTargets')).get('targetInfos', [])
                    pages = [target for target in targets
                             if target.get('targetId') == target_id and is_mexc(target)]
                for page in pages:
                    page_id = page.get('targetId')
                    if not page_id:
                        continue
                    if page_id not in sessions:
                        attached = await call('Target.attachToTarget',
                                              {'targetId': page_id, 'flatten': True})
                        sessions[page_id] = attached.get('sessionId')
                    if not sessions[page_id]:
                        continue
                    try:
                        result = await call('Runtime.evaluate', {
                            'expression': "({ready:document.readyState,bodyChars:(document.body?.innerText||'').trim().length})",
                            'returnByValue': True}, sessions[page_id])
                    except AdsPowerTimeout:
                        continue
                    value = result.get('result', {}).get('value', {})
                    if (isinstance(value, dict) and value.get('ready') in {'interactive', 'complete'}
                            and isinstance(value.get('bodyChars'), int) and value['bodyChars'] > 0):
                        return
                await asyncio.sleep(2)
            raise AdsPowerUnavailable('Вкладка MEXC не загрузила содержимое страницы за 30 секунд; '
                                      'проверьте браузер и прокси профиля')

    async def stop_profile(self) -> None:
        """Release a temporary maker profile after its completed cycle."""
        if not self.api_key or not self.profile_id:
            raise AdsPowerError('Для закрытия профиля нужны ключ AdsPower и его ID')
        try:
            async with httpx.AsyncClient(timeout=max(15, self.command_timeout), trust_env=False) as client:
                response = await client.get(self.base_url + '/api/v1/browser/stop',
                    params={'user_id': self.profile_id},
                    headers={'Authorization': 'Bearer ' + self.api_key})
                payload = response.json()
        except (httpx.RequestError, ValueError) as exc:
            raise AdsPowerUnavailable(f'Не удалось закрыть временный профиль AdsPower ({type(exc).__name__})') from None
        if response.status_code != 200 or not isinstance(payload, dict) or payload.get('code') != 0:
            raise AdsPowerError('AdsPower не подтвердил закрытие временного профиля')

    async def close_other_local_profiles(self, keep_profile_ids=None) -> int:
        """Keep the static P1 and any active trading-role browser profiles."""
        if os.getenv('ADSPOWER_CLOSE_OTHER_PROFILES', 'true').strip().lower() in {'0', 'false', 'no', 'off'}:
            return 0
        if not self.api_key or not self.profile_id:
            raise AdsPowerError("Для контроля профилей нужны ADSPOWER_API_KEY и ADSPOWER_P1_PROFILE_ID")
        headers = {"Authorization": "Bearer " + self.api_key}
        async with httpx.AsyncClient(timeout=max(10, self.command_timeout), trust_env=False) as client:
            try:
                response = await client.get(self.base_url + "/api/v1/browser/local-active", headers=headers)
                payload = response.json()
            except (httpx.RequestError, ValueError) as exc:
                raise AdsPowerUnavailable(f"Не удалось прочитать открытые профили AdsPower ({type(exc).__name__})") from None
            data = payload.get("data") if isinstance(payload, dict) else None
            entries = data.get("list") if isinstance(data, dict) else None
            if (response.status_code != 200 or not isinstance(payload, dict)
                    or payload.get("code") != 0 or not isinstance(entries, list)):
                raise AdsPowerError("AdsPower не вернул список открытых профилей")
            if any(not isinstance(item, dict) or not isinstance(item.get("user_id"), str)
                   or not item["user_id"] for item in entries):
                raise AdsPowerError("AdsPower вернул некорректный список открытых профилей")
            opened = {item["user_id"] for item in entries}
            protected = {self.profile_id, *(keep_profile_ids or ())}
            if not (opened & protected):
                raise AdsPowerError("Профиль П1 не открыт в локальном AdsPower; другие профили не закрывались")
            closed = 0
            for profile_id in sorted(opened - protected):
                try:
                    stopped = await client.get(self.base_url + "/api/v1/browser/stop",
                                               params={"user_id": profile_id}, headers=headers)
                    body = stopped.json()
                except (httpx.RequestError, ValueError) as exc:
                    raise AdsPowerUnavailable(f"Не удалось закрыть лишний профиль AdsPower ({type(exc).__name__})") from None
                if stopped.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
                    raise AdsPowerError("AdsPower не подтвердил закрытие лишнего профиля")
                closed += 1
            return closed

    async def endpoint(self) -> str:
        if not self.profile_id or not self.api_key:
            raise AdsPowerError("Заполните ADSPOWER_P1_PROFILE_ID и ADSPOWER_API_KEY в .env")
        async with httpx.AsyncClient(timeout=max(10, self.command_timeout), trust_env=False) as client:
            for attempt in range(3):
                try:
                    response = await client.get(self.base_url + "/api/v1/browser/active",
                        params={"user_id": self.profile_id}, headers={"Authorization": "Bearer " + self.api_key})
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    if attempt == 2:
                        raise AdsPowerUnavailable(
                            f"AdsPower Local API временно недоступен ({type(exc).__name__})") from None
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if response.status_code != 200:
                    raise AdsPowerError(f"AdsPower HTTP {response.status_code}: проверьте Local API и ключ")
                payload = response.json()
                if payload.get("code") == -1:
                    if attempt == 2:
                        raise AdsPowerUnavailable(
                            "AdsPower временно отклонил проверку профиля (код -1)")
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if payload.get("code") != 0:
                    raise AdsPowerError("AdsPower отклонил проверку профиля")
                data = payload.get("data", {})
                if data.get("status") != "Active":
                    raise AdsPowerError("Откройте профиль П1 в AdsPower и нужный ордер MEXC")
                return local_url(data.get("ws", {}).get("puppeteer", ""), {"ws", "wss"})

    @asynccontextmanager
    async def connection(self):
        connected = False
        try:
            async with websockets.connect(await self.endpoint(), open_timeout=self.command_timeout, close_timeout=3,
                                          proxy=None) as ws:
                connected = True
                seq = 0

                async def call(method, params=None, session=None, *, timeout=None):
                    nonlocal seq
                    if timeout is None:
                        timeout = self.command_timeout
                    seq += 1
                    request_id = seq
                    request = {"id": request_id, "method": method, "params": params or {}}
                    if session:
                        request["sessionId"] = session
                    async def send_and_receive():
                        await ws.send(json.dumps(request))
                        while True:
                            response = json.loads(await ws.recv())
                            if not isinstance(response, dict):
                                raise AdsPowerError(f"AdsPower: некорректный ответ CDP на {method}")
                            if response.get("id") == request_id:
                                if "error" in response:
                                    raise AdsPowerError(f"Браузер отклонил команду {method}; проверьте открытую вкладку")
                                result = response.get("result")
                                if not isinstance(result, dict):
                                    raise AdsPowerError(f"AdsPower: некорректный результат CDP для {method}")
                                return result
                    try:
                        # asyncio.timeout is unavailable on the server's Python 3.10.
                        return await asyncio.wait_for(send_and_receive(), timeout=timeout)
                    except (asyncio.TimeoutError, TimeoutError):
                        raise AdsPowerTimeout(f"AdsPower: команда {method} не ответила за {timeout} секунд") from None
                yield call
        except AdsPowerError:
            raise
        except Exception as exc:
            # Never include raw websocket URLs, headers, tokens, page contents or cookies.
            location = _safe_error_location(exc)
            message = (f"Ошибка AdsPower ({type(exc).__name__}, {location}); "
                       "проверьте результат на MEXC")
            if not connected and isinstance(exc, (OSError, TimeoutError)):
                # No CDP command was sent, so the scheduler may safely retry its preflight.
                raise AdsPowerUnavailable(message) from None
            raise AdsPowerError(message) from None
        # Closing this CDP connection detaches the automation; the browser stays open.

    async def view(self, call, session, order_no, *, click=False):
        expression = f"({ORDER_VIEW})({json.dumps(order_no)}, {json.dumps(click)})"
        try:
            request = {"expression": expression, "returnByValue": True}
            if click:
                # A slow headless page may process the click before answering CDP.
                response = await call("Runtime.evaluate", request, session, timeout=30)
            else:
                response = await call("Runtime.evaluate", request, session)
        except AdsPowerTimeout:
            if click:
                raise AdsPowerClickUnknown("AdsPower не ответил после команды нажатия. Результат неизвестен; повторного нажатия не будет") from None
            raise
        if "exceptionDetails" in response:
            raise AdsPowerError("Не удалось проверить окно ордера MEXC")
        return response.get("result", {}).get("value", {}).get("state", "blocked")

    async def read_view(self, call, session, target_id, order_no):
        try:
            return await self.view(call, session, order_no)
        except AdsPowerTimeout:
            # AdsPower background tabs may stop responding. Only retry a read;
            # never queue another click after a timeout.
            await call("Target.activateTarget", {"targetId": target_id})
            try:
                return await self.view(call, session, order_no)
            except AdsPowerTimeout:
                raise AdsPowerError("Вкладка MEXC не отвечает даже после переключения на неё. Кнопка проверки не нажималась") from None

    async def locate(self, call, order_no, *, allow_missing=False):
        if not re.fullmatch(r"d\d{15,25}", order_no):
            raise AdsPowerError("Некорректный номер ордера для проверки документов")
        matches = []
        merchant_matches = []
        for target in (await call("Target.getTargets")).get("targetInfos", []):
            url = urlsplit(target.get("url", ""))
            if (target.get("type") != "page" or url.scheme != "https"
                    or url.hostname not in {"www.mexc.com", "mexc.com"}
                    or not (url.path.endswith("/buy-crypto/control") or
                            (url.path.endswith("/buy-crypto/order-processing") and
                             parse_qs(url.query).get("id") == [order_no]))):
                continue
            session = (await call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            state = await self.read_view(call, session, target["targetId"], order_no)
            if state != "missing":
                matches.append((session, state))
                if url.path.endswith('/buy-crypto/control'):
                    merchant_matches.append((session, state))
        matches = merchant_matches or matches
        if not matches and allow_missing:
            return None
        if len(matches) != 1 or matches[0][1] not in {"ready", "passed"}:
            raise AdsPowerError(f"Откройте единственное окно ордера {order_no} в портале мерчанта профиля П1; "
                                "ожидается «Ожидание проверки» и активная кнопка «Проверка пройдена»")
        return matches[0]

    async def open_order(self, order_no: str) -> str:
        """Open the existing order, without approving verification or changing it."""
        async with self.connection() as call:
            found = await self.locate(call, order_no, allow_missing=True)
            if found:
                session, state = found
                if state != "passed":
                    return state
                verified = await self.verification_state(call, session, order_no)
                if verified in {"passed", "not_required"}:
                    return verified
                return await self.open_merchant_order(call, order_no)
            targets = (await call("Target.getTargets")).get("targetInfos", [])
            existing = [t for t in targets if t.get("type") == "page"
                        and urlsplit(t.get("url", "")).scheme == "https"
                        and urlsplit(t.get("url", "")).hostname in {"mexc.com", "www.mexc.com"}
                        and urlsplit(t["url"]).path.endswith("/buy-crypto/order-processing")
                        and parse_qs(urlsplit(t["url"]).query).get("id") == [order_no]]
            if len(existing) > 1:
                raise AdsPowerError("Открыто несколько вкладок этого ордера; оставьте одну")
            target = existing[0] if existing else await call("Target.createTarget", {
                "url": f"https://www.mexc.com/ru-RU/buy-crypto/order-processing?id={order_no}",
                "background": False})
            session = (await call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            for _ in range(20):
                state = await self.read_view(call, session, target["targetId"], order_no)
                if state in {"ready", "passed"}:
                    if state == "passed":
                        verified = await self.verification_state(call, session, order_no)
                        if verified not in {"passed", "not_required"}:
                            return await self.open_merchant_order(call, order_no)
                        return verified
                    return state
                await asyncio.sleep(1)
            # The standalone page can omit the merchant verification dialog.
            # Read the exact order on MEXC before deciding whether a click is needed.
            verified = await self.verification_state(call, session, order_no)
            if verified in {"passed", "not_required"}:
                return verified
            if verified == "ready":
                return await self.open_merchant_order(call, order_no)
            raise AdsPowerError("MEXC не подтвердил, требуется ли проверка документов; "
                                "кнопка не нажималась. Сверьте ордер в портале мерчанта П1")

    async def inspect(self, order_no: str) -> str:
        async with self.connection() as call:
            session, state = await self.locate(call, order_no)
            return await self.verification_state(call, session, order_no) if state == "passed" else state

    async def close_order_tabs(self, order_nos: list[str]) -> int:
        """Close only exact standalone order pages, never the shared merchant portal."""
        if not order_nos or any(not re.fullmatch(r"d\d{15,25}", n) for n in order_nos):
            raise AdsPowerError("Некорректные номера завершённых ордеров для закрытия вкладок")

        def matches(target):
            url = urlsplit(target.get("url", ""))
            ids = parse_qs(url.query, keep_blank_values=True).get("id", [])
            return (target.get("type") == "page" and url.scheme == "https"
                    and url.hostname in {"www.mexc.com", "mexc.com"}
                    and url.path.endswith("/buy-crypto/order-processing")
                    and len(ids) == 1 and ids[0] in order_nos)

        closed = 0
        async with self.connection() as call:
            for target in (await call("Target.getTargets"))["targetInfos"]:
                if not matches(target):
                    continue
                # Do not close a tab that navigated to another page since enumeration.
                fresh = (await call("Target.getTargetInfo", {"targetId": target["targetId"]}))["targetInfo"]
                if not matches(fresh) or fresh["url"] != target["url"]:
                    continue
                result = await call("Target.closeTarget", {"targetId": target["targetId"]})
                if result.get("success") is not True:
                    raise AdsPowerError("Не удалось закрыть вкладку завершённого ордера")
                closed += 1
        return closed

    async def ad_details(self, adv_no: str) -> dict:
        return await self._ad_quantity(adv_no)

    async def replenish_ad(self, plan: dict) -> dict:
        quantity, before, target = (Decimal(plan[k]) for k in ('quantity', 'before_available', 'target_available'))
        if (not all(n.is_finite() for n in (quantity, before, target))
                or quantity <= 0 or before < 0 or before + quantity != target):
            raise AdsPowerError("Некорректный план пополнения; запрос не отправлен")
        if 'target_total' in plan:
            frozen = Decimal(plan['before_frozen'])
            total = Decimal(plan['target_total'])
            if (not frozen.is_finite() or frozen < 0 or not total.is_finite()
                    or before + frozen + quantity != total):
                raise AdsPowerError("Некорректный общий остаток объявления; запрос не отправлен")
        return await self._ad_quantity(plan['adv_no'], plan)

    async def _ad_quantity(self, adv_no: str, plan: dict | None = None) -> dict:
        if not re.fullmatch(r"a\d{15,25}", adv_no):
            raise AdsPowerError("Некорректный номер объявления")
        async with self.connection() as call:
            targets = (await call('Target.getTargets')).get('targetInfos', [])
            targets = [t for t in targets if t.get('type') == 'page'
                       and urlsplit(t.get('url', '')).scheme == 'https'
                       and urlsplit(t.get('url', '')).hostname in {'mexc.com', 'www.mexc.com'}]
            if not targets:
                raise AdsPowerError("Откройте MEXC в профиле П1 AdsPower")
            target = next((t for t in targets if 'create-advertising' in urlsplit(t['url']).path), targets[0])
            await call('Target.activateTarget', {'targetId': target['targetId']})
            session = (await call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True}))['sessionId']
            response = await call('Runtime.evaluate', {'expression': f'({AD_QUANTITY})({json.dumps(adv_no)}, {json.dumps(plan)})',
                                 'awaitPromise': True, 'returnByValue': True}, session)
            value = response.get('result', {}).get('value')
            if 'exceptionDetails' in response or not isinstance(value, dict) or value.get('id') != adv_no:
                reason = value.get('error', 'read_failed') if isinstance(value, dict) else 'read_failed'
                raise AdsPowerError(f"MEXC: пополнение/чтение объявления через браузер не подтверждено ({reason}). "
                                    "Повторного запроса не будет; проверьте остаток и дополнительную проверку на сайте")
            return value

    async def verification_state(self, call, session, order_no: str) -> str:
        # Read within P1's existing session. Never export cookies or the full response.
        if not re.fullmatch(r"d\d{15,25}", order_no):
            raise AdsPowerError("Некорректный номер ордера")
        expression = r"""(async(orderNo) => {
            if (location.protocol !== 'https:' || !['mexc.com','www.mexc.com'].includes(location.hostname)) return null;
            const response = await fetch('/api/platform/p2p/api/merchant/order_deal/info/' + orderNo,
                {method:'GET',credentials:'same-origin',cache:'no-store'});
            if (!response.ok) return null;
            const body = await response.json(), data = body.data;
            if (body.code !== 0 || !data || data.id !== orderNo) return null;
            return {state:data.overVerifyState,
                required:Object.prototype.hasOwnProperty.call(data,'overVerify') ? !!data.overVerify : null};
        })""" + f"({json.dumps(order_no)})"
        response = await call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, session)
        value = response.get("result", {}).get("value")
        if "exceptionDetails" in response or not isinstance(value, dict):
            raise AdsPowerError("Сервер MEXC не подтвердил состояние дополнительной проверки; продолжение остановлено")
        state, required = value.get('state'), value.get('required')
        if type(state) is int and state == 1:
            return "passed"
        if type(state) is int and state == 0 and type(required) is bool:
            return "ready" if required else "not_required"
        if type(state) is int and state == 0 and required is None:
            return "unknown"
        raise AdsPowerError("MEXC не сообщил однозначно, требуется ли проверка документов в этом ордере. "
                            "Статус 0 не доказывает ожидание проверки. Сверьте ордер в портале мерчанта П1")

    async def server_verification_state(self, order_no: str) -> str:
        """Read the exact order's server state even if its visible tab is stalled."""
        async with self.connection() as call:
            targets = (await call("Target.getTargets")).get("targetInfos", [])
            pages = [target for target in targets if target.get("type") == "page"
                     and urlsplit(target.get("url", "")).hostname in {"mexc.com", "www.mexc.com"}]
            pages.sort(key=lambda target: (order_no not in target.get("url", ""),
                                           "/buy-crypto/control" not in target.get("url", "")))
            saw_ready = False
            saw_not_required = False
            for target in pages[:5]:
                try:
                    attached = await call("Target.attachToTarget", {
                        "targetId": target["targetId"], "flatten": True})
                    state = await self.verification_state(call, attached["sessionId"], order_no)
                except AdsPowerError:
                    continue
                if state == "passed":
                    return state
                if state == "ready":
                    saw_ready = True
                if state == "not_required":
                    saw_not_required = True
            if saw_ready:
                return "ready"
            if saw_not_required:
                return "not_required"
            raise AdsPowerError("Не удалось прочитать состояние проверки ордера на MEXC через профиль П1")

    async def open_merchant_order(self, call, order_no: str) -> str:
        """The standalone page can hide pending verification. Open the exact maker row."""
        def merchant_page(target):
            url = urlsplit(target.get('url', ''))
            return (target.get('type') == 'page' and url.scheme == 'https'
                    and url.hostname in {'mexc.com', 'www.mexc.com'}
                    and url.path.endswith('/buy-crypto/control'))

        targets = [target for target in (await call('Target.getTargets')).get('targetInfos', [])
                   if merchant_page(target)]
        if not targets:
            created = await call('Target.createTarget', {
                'url': 'https://www.mexc.com/ru-RU/buy-crypto/control',
                'background': False})
            if not created.get('targetId'):
                raise AdsPowerError('AdsPower не открыл портал мерчанта П1; кнопка не нажималась')
            targets = [{'type': 'page', 'targetId': created['targetId'],
                        'url': 'https://www.mexc.com/ru-RU/buy-crypto/control'}]
        for target in targets:
            session = (await call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            expression = r"""(orderNo => {
                const rows = Array.from(document.querySelectorAll('[data-row-key]'))
                    .filter(e => e.getAttribute('data-row-key') === orderNo && e.getClientRects().length);
                if (rows.length !== 1) return false;
                const icons = rows[0].querySelectorAll('svg[class*="ControlOrders_contractIcon"]');
                if (icons.length !== 1) return false;
                icons[0].dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true})); return true;
            })""" + f"({json.dumps(order_no)})"
            for _ in range(15):
                result = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
                if result.get("result", {}).get("value") is True:
                    break
                await asyncio.sleep(1)
            else:
                continue
            for _ in range(10):
                state = await self.read_view(call, session, target["targetId"], order_no)
                if state == "ready":
                    return state
                await asyncio.sleep(1)
            break
        raise AdsPowerError(f"Кнопка проверки ордера {order_no} недоступна в портале мерчанта П1. "
                            "Не удалось подтвердить, требуется ли проверка; оплата не отправлена")

    async def approve(self, order_no: str) -> None:
        click_attempted = False
        try:
            async with self.connection() as call:
                session, state = await self.locate(call, order_no)
                if state == "passed":
                    if await self.verification_state(call, session, order_no) in {"passed", "not_required"}:
                        return
                    raise AdsPowerError("Заголовок показывает ожидание оплаты, но проверка на сервере ещё не пройдена. Откройте ордер в портале мерчанта П1")
                # Recheck the exact order and state in the same JS operation as the click.
                click_attempted = True
                if await self.view(call, session, order_no, click=True) != "clicked":
                    raise AdsPowerError("Окно ордера изменилось; нажатие не выполнено")
                for _ in range(15):
                    await asyncio.sleep(1)
                    if (await self.view(call, session, order_no) == "passed"
                            and await self.verification_state(call, session, order_no) == "passed"):
                        return
                raise AdsPowerClickUnknown("Нажатие выполнено, но переход к ожиданию оплаты не подтверждён. "
                                           "Проверьте MEXC; автоматического повторного нажатия не будет")
        except AdsPowerError:
            if not click_attempted:
                raise
            # The click may already have succeeded. Reconnect and only read MEXC;
            # never send another click while reconciling an unknown result.
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(2)
                try:
                    if await self.server_verification_state(order_no) == "passed":
                        return
                except AdsPowerError:
                    continue
            raise
