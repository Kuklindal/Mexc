"""Attach to P1's AdsPower profile; only approve the explicitly confirmed order."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
from decimal import Decimal
from urllib.parse import urlsplit, parse_qs

import httpx
import websockets


class AdsPowerError(RuntimeError):
    pass


class AdsPowerTimeout(AdsPowerError):
    pass


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
        return {id:d.id, availableQuantity:d.availableQuantity, overVerify:d.overVerify ?? null,
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
    if (before.coinName !== 'USDT' || before.currency !== plan.fiat || before.tradeType !== 1
            || Number(before.availableQuantity) !== Number(plan.before_available)
            || verification(before.overVerify) !== verification(plan.over_verify)) return {error:'ad_changed'};
    const response = await fetch(root + '/quantity', {method:'POST',credentials:'same-origin',
        body:new URLSearchParams({id:advNo,quantity:plan.quantity})});
    const body = await response.json();
    if (!response.ok || body.code !== 0) return {error:'quantity_rejected',code:body.code,http:response.status};
    const after = await read();
    if (Number(after.availableQuantity) !== Number(plan.target_available)
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


def local_url(value: str, schemes: set[str]) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise AdsPowerError("AdsPower должен использовать локальный адрес 127.0.0.1 или localhost")
    return value


class AdsPower:
    def __init__(self, base_url: str, api_key: str, profile_id: str):
        self.base_url = local_url(base_url.rstrip('/'), {"http", "https"})
        self.api_key, self.profile_id = api_key.strip(), profile_id.strip()

    @classmethod
    def from_env(cls):
        return cls(os.getenv("ADSPOWER_BASE_URL", "http://127.0.0.1:50325"),
                   os.getenv("ADSPOWER_API_KEY", ""), os.getenv("ADSPOWER_P1_PROFILE_ID", ""))

    async def endpoint(self) -> str:
        if not self.profile_id or not self.api_key:
            raise AdsPowerError("Заполните ADSPOWER_P1_PROFILE_ID и ADSPOWER_API_KEY в .env")
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(self.base_url + "/api/v1/browser/active",
                params={"user_id": self.profile_id}, headers={"Authorization": "Bearer " + self.api_key})
        if response.status_code != 200:
            raise AdsPowerError(f"AdsPower HTTP {response.status_code}: проверьте Local API и ключ")
        payload = response.json()
        if payload.get("code") != 0:
            raise AdsPowerError("AdsPower отклонил запрос: проверьте ключ и ID профиля П1")
        data = payload.get("data", {})
        if data.get("status") != "Active":
            raise AdsPowerError("Откройте профиль П1 в AdsPower и нужный ордер MEXC")
        return local_url(data.get("ws", {}).get("puppeteer", ""), {"ws", "wss"})

    @asynccontextmanager
    async def connection(self):
        try:
            async with websockets.connect(await self.endpoint(), open_timeout=10, close_timeout=3) as ws:
                seq = 0

                async def call(method, params=None, session=None):
                    nonlocal seq
                    seq += 1
                    request_id = seq
                    request = {"id": request_id, "method": method, "params": params or {}}
                    if session:
                        request["sessionId"] = session
                    try:
                        async with asyncio.timeout(10):
                            await ws.send(json.dumps(request))
                            while True:
                                response = json.loads(await ws.recv())
                                if response.get("id") == request_id:
                                    if "error" in response:
                                        raise AdsPowerError(f"Браузер отклонил команду {method}; проверьте открытую вкладку")
                                    return response.get("result", {})
                    except TimeoutError:
                        raise AdsPowerTimeout(f"AdsPower: команда {method} не ответила за 10 секунд") from None
                yield call
        except AdsPowerError:
            raise
        except Exception as exc:
            # Never include raw websocket URLs, headers, tokens, page contents or cookies.
            raise AdsPowerError(f"Ошибка подключения к AdsPower ({type(exc).__name__}); проверьте результат на MEXC") from None
        # Closing this CDP connection detaches the automation; the browser stays open.

    async def view(self, call, session, order_no, *, click=False):
        expression = f"({ORDER_VIEW})({json.dumps(order_no)}, {json.dumps(click)})"
        try:
            response = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
        except AdsPowerTimeout:
            if click:
                raise AdsPowerError("AdsPower не ответил после команды нажатия. Результат неизвестен; повторного нажатия не будет") from None
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
            raise AdsPowerError("Ордер открыт, но кнопка проверки недоступна. Проверьте вход П1 и состояние страницы MEXC; действие не выполнено")

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

    async def open_merchant_order(self, call, order_no: str) -> str:
        """The standalone page can hide pending verification. Open the exact maker row."""
        for target in (await call("Target.getTargets")).get("targetInfos", []):
            url = urlsplit(target.get("url", ""))
            if (target.get("type") != "page" or url.scheme != "https"
                    or url.hostname not in {"mexc.com", "www.mexc.com"}
                    or not url.path.endswith('/buy-crypto/control')):
                continue
            session = (await call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            expression = r"""(orderNo => {
                const rows = Array.from(document.querySelectorAll('[data-row-key]'))
                    .filter(e => e.getAttribute('data-row-key') === orderNo && e.getClientRects().length);
                if (rows.length !== 1) return false;
                const icons = rows[0].querySelectorAll('svg[class*="ControlOrders_contractIcon"]');
                if (icons.length !== 1) return false;
                icons[0].dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true})); return true;
            })""" + f"({json.dumps(order_no)})"
            result = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
            if result.get("result", {}).get("value") is not True:
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
        async with self.connection() as call:
            session, state = await self.locate(call, order_no)
            if state == "passed":
                if await self.verification_state(call, session, order_no) in {"passed", "not_required"}:
                    return
                raise AdsPowerError("Заголовок показывает ожидание оплаты, но проверка на сервере ещё не пройдена. Откройте ордер в портале мерчанта П1")
            # Recheck the exact order and state in the same JS operation as the click.
            if await self.view(call, session, order_no, click=True) != "clicked":
                raise AdsPowerError("Окно ордера изменилось; нажатие не выполнено")
            for _ in range(15):
                await asyncio.sleep(1)
                if (await self.view(call, session, order_no) == "passed"
                        and await self.verification_state(call, session, order_no) == "passed"):
                    return
            raise AdsPowerError("Нажатие выполнено, но переход к ожиданию оплаты не подтверждён. "
                                "Проверьте MEXC; автоматического повторного нажатия не будет")
