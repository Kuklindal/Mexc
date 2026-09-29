from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
import logging
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode, quote

import httpx
import websockets
from python_socks.async_.asyncio import Proxy


class MexcAPIError(RuntimeError):
    def __init__(self, message: str, *, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class MexcReadUnavailable(MexcAPIError):
    """A read-only request failed; no exchange state change was requested."""


def counterparty_identity(detail: dict) -> tuple[str, str]:
    info = detail.get("userInfo") or detail.get("merchantInfo") or {}
    if not isinstance(info, dict):
        return "", ""
    member, nickname = info.get("memberId"), info.get("nickName")
    return (str(member) if member is not None else "",
            nickname if isinstance(nickname, str) else "")


def ad_verification(ad: dict, fallback: str = "") -> str:
    """The ads list can omit overVerify; omission must never disable verification."""
    value = ad.get("overVerify")
    if value is None:
        value = fallback
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        raise ValueError('Дополнительная проверка объявления не подтверждена. Проверьте, что на MEXC включено и сохранено «Удостоверение личности»; запрос не отправлен')
    types = data.get("types")
    if (set(data) - {"types", "otherText"} or not isinstance(types, list)
            or not 1 <= len(types) <= 3 or any(type(t) is not int or t not in range(1, 7) for t in types)
            or len(set(types)) != len(types)):
        raise ValueError("Некорректный overVerify: нужны 1–3 разных типа проверки от 1 до 6; пополнение не отправлено")
    result = {"types": sorted(types)}
    if 6 in types:
        if not isinstance(data.get("otherText"), str) or not data["otherText"].strip():
            raise ValueError("Для типа проверки 6 требуется otherText; пополнение не отправлено")
        result["otherText"] = data["otherText"]
    elif data.get("otherText"):
        raise ValueError("otherText допустим только для типа проверки 6; пополнение не отправлено")
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def ad_replenish_params(ad: dict, quantity: str) -> dict:
    """Add supplyQuantity, capping the order maximum to the resulting fiat value."""
    required = ("advNo", "payTimeLimit", "price", "coinId", "side", "fiatUnit",
                "minSingleTransAmount", "maxSingleTransAmount", "userAllTradeCountMin", "userAllTradeCountMax")
    if any(ad.get(key) is None for key in required) or ad.get("availableQuantity") is None:
        raise ValueError("MEXC не вернул обязательные параметры объявления для пополнения")
    if ad["side"] != "SELL" or ad.get("coinName") != "USDT":
        raise ValueError("Пополнять можно только объявление П1 о продаже USDT")
    try:
        available, increment = Decimal(str(ad["availableQuantity"])), Decimal(str(quantity))
    except InvalidOperation:
        raise ValueError("Некорректное количество USDT для пополнения") from None
    if not available.is_finite() or available < 0 or not increment.is_finite() or increment <= 0:
        raise ValueError("Остаток должен быть неотрицательным, а пополнение — положительным")
    try:
        price, minimum, maximum = (Decimal(str(ad[k])) for k in
                                   ("price", "minSingleTransAmount", "maxSingleTransAmount"))
    except InvalidOperation:
        raise ValueError("Некорректная цена или лимиты объявления") from None
    if any(not n.is_finite() or n <= 0 for n in (price, minimum, maximum)) or minimum > maximum:
        raise ValueError("Некорректная цена или лимиты объявления")
    total = ((available + increment) * price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if minimum > total:
        raise ValueError("Стоимость объёма после пополнения меньше минимального лимита сделки. Проверьте лимиты объявления; запрос не отправлен")
    payments = ad.get("paymentInfo") or []
    if not payments or any(not isinstance(p, dict) or not str(p.get("id", "")).isdigit()
                           or int(p["id"]) <= 0 for p in payments):
        raise ValueError("MEXC не вернул ID реквизитов объявления П1")
    params = {key: ad[key] for key in required}
    params["overVerify"] = ad_verification(ad)
    if maximum > total:
        params["maxSingleTransAmount"] = format(total, "f")
    # quantity from the listing may include historical turnover and exceed the
    # ad limit. The required initQuantity is validated even on updates; use the
    # intended available amount. Only supplyQuantity specifies the increment.
    params.update(initQuantity=format(available + increment, "f"), supplyQuantity=format(increment, "f"),
                  payMethod=",".join(str(p["id"]) for p in payments))
    for key in ("countryCode", "autoReplyMsg", "tradeTerms", "kycLevel", "maxPayLimit", "buyerRegDaysLimit",
                "priceType", "priceRatio", "supportKycCountry", "merchantTradeEnable",
                "onlyTradeKybUser", "display", "adsType", "adDisplayAreaType", "securityOrderPaymentInfo"):
        if ad.get(key) is not None:
            params[key] = ad[key]
    # advStatus is deliberately omitted: replenishment does not publish a closed ad.
    return params


class MexcP2PClient:
    """
    Minimal async client for documented MEXC P2P endpoints.

    MEXC P2P docs state that signing is the same as Spot API:
    HMAC-SHA256 over the exact query string, API key in X-MEXC-APIKEY.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = "https://api.mexc.com",
        recv_window: int = 5000,
        proxy_url: str | None = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self.server_offset_ms = 0
        self.proxy_url = proxy_url
        self.logger = logging.getLogger("mexc_p2p.api")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"X-MEXC-APIKEY": self.api_key},
            proxy=proxy_url,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _signed_query(self, params: dict[str, Any] | None = None) -> str:
        items: list[tuple[str, str]] = []

        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            items.append((key, str(value)))

        items.append(("recvWindow", str(self.recv_window)))
        items.append(("timestamp", str(int(time.time() * 1000) + self.server_offset_ms)))

        # MEXC rejects signatures using '+' for spaces; sign and send the same
        # percent-encoded bytes, including %20 for spaces and %2B for literal '+'.
        query = urlencode(items, doseq=True, quote_via=quote, safe="")
        signature = hmac.new(
            self.secret_key,
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.logger.debug("%s %s", method.upper(), path)
        for attempt in range(2):
            query = self._signed_query(params)
            url = f"{self.base_url}{path}?{query}"
            try:
                response = await self.http.request(method.upper(), url)
            except httpx.RequestError as exc:
                if method.upper() == "GET":
                    raise MexcReadUnavailable(
                        f"MEXC read request failed ({type(exc).__name__}); no action was sent") from None
                raise MexcAPIError(f"MEXC network error ({type(exc).__name__}); execution may be unknown") from None

            # Never retry a network/5xx failure on a state-changing endpoint.
            try:
                payload = response.json()
            except Exception:
                raise MexcAPIError(f"Non-JSON response: HTTP {response.status_code}")
            if (attempt == 0 and response.status_code < 500 and isinstance(payload, dict)
                    and payload.get('code') == 700003):
                try:
                    await self._sync_server_time()
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    pass  # Keep the original explicit rejection below.
                else:
                    continue
            break

        if response.status_code >= 400:
            raise MexcAPIError(
                f"{method.upper()} {path}: HTTP {response.status_code}: {json.dumps(payload, ensure_ascii=False)}",
                code=payload.get("code") if isinstance(payload, dict) else None, http_status=response.status_code,
            )

        if isinstance(payload, dict) and "code" in payload and payload.get("code") not in (0, None):
            raise MexcAPIError(
                f"{method.upper()} {path}: MEXC error: {json.dumps(payload, ensure_ascii=False)}",
                code=payload.get("code"), http_status=response.status_code,
            )

        return payload

    async def list_orders(
        self,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        coin_id: str | None = None,
        side: str | None = None,
        order_state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            "/api/v3/fiat/market/order/paginationV2",
            {
                "coinId": coin_id,
                "side": side,
                "orderDealState": order_state,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "limit": limit,
            },
        )
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            raise MexcAPIError("Unexpected order list response: data must be a list")
        return data

    async def get_order_detail(self, order_no: str) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/api/v3/fiat/order/detail",
            {"advOrderNo": order_no},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise MexcAPIError(f"Unexpected order detail response for {order_no}")
        return data

    async def list_active_maker_orders(self) -> list[dict]:
        end = int(time.time() * 1000)
        params = {"startTime": end - 30 * 86400000, "endTime": end, "limit": 50,
                  "orderDealState": "NOT_PAID,PAID,WAIT_PROCESS,PROCESSING"}
        orders, seen = [], set()
        for _ in range(100):
            payload = await self._request("GET", "/api/v3/fiat/merchant/order/paginationV2", params)
            page = payload.get("data")
            if not isinstance(page, list):
                raise MexcAPIError("Не удалось проверить список активных ордеров П1")
            for order in page:
                number = order.get("advOrderNo")
                if not number or number in seen:
                    raise MexcAPIError("Некорректная пагинация активных ордеров; автоматические действия остановлены")
                seen.add(number)
                orders.append(order)
            if len(page) < 50:
                return orders
            params.update(lastId=page[-1]["advOrderNo"], lastCreateTime=page[-1]["createTime"])
        raise MexcAPIError("Слишком много страниц активных ордеров; проверка не завершена")

    async def get_ad(self, adv_no: str) -> dict[str, Any]:
        payload = await self._request("GET", "/api/v3/fiat/merchant/ads/pagination",
                                      {"advNo": adv_no, "advStatus": "OPEN,CLOSE,LOW_STOCK", "page": 1, "limit": 10})
        ads = payload.get("data") if isinstance(payload, dict) else None
        page = payload.get("page") if isinstance(payload, dict) else None
        if ads is None and isinstance(page, dict) and type(page.get("total")) is int and page["total"] == 0:
            ads = []  # MEXC omits data for an empty page.
        if not isinstance(ads, list):
            raise MexcAPIError(f"MEXC вернул некорректный список объявлений: data={type(ads).__name__}. "
                               f"Не удалось прочитать объявление {adv_no}")
        matches = [ad for ad in ads if isinstance(ad, dict) and ad.get("advNo") == adv_no]
        if len(matches) != 1:
            raise MexcAPIError(f"Объявление {adv_no} не найдено однозначно среди объявлений П1 "
                               "со статусами OPEN/CLOSE/LOW_STOCK. Проверьте номер и доступность объявления")
        return matches[0]

    async def replenish_ad(self, ad: dict, quantity: str) -> None:
        payload = await self._request("POST", "/api/v3/fiat/merchant/ads/save_or_update",
                                      ad_replenish_params(ad, quantity))
        if not isinstance(payload, dict) or payload.get("data") != ad["advNo"]:
            raise MexcAPIError("MEXC не подтвердил номер пополненного объявления; требуется сверка")

    async def transfer_usdt(self, source: str, target: str, amount: str) -> str:
        if {source, target} != {"OTC", "SPOT"}:
            raise ValueError("Разрешён перевод только между OTC и SPOT")
        try:
            quantity = Decimal(amount)
        except InvalidOperation:
            raise ValueError("Некорректное количество USDT") from None
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("Количество USDT должно быть положительным")
        payload = await self._request("POST", "/api/v3/capital/transfer", {
            "fromAccountType": source, "toAccountType": target,
            "asset": "USDT", "amount": format(quantity, "f")})
        if isinstance(payload, list) and len(payload) == 1:
            payload = payload[0]
        if not isinstance(payload, dict) or not isinstance(payload.get("tranId"), str) or not payload["tranId"].strip():
            raise MexcAPIError("MEXC не вернул tranId перевода; требуется сверка истории, повтор запрещён")
        return payload["tranId"]

    async def get_wallet_transfer(self, tran_id: str) -> dict:
        payload = await self._request("GET", "/api/v3/capital/transfer/tranId", {"tranId": tran_id})
        if not isinstance(payload, dict) or payload.get("tranId") != tran_id:
            raise MexcAPIError("MEXC вернул неожиданный результат проверки перевода")
        return payload

    async def _sync_server_time(self) -> None:
        before = time.time() * 1000
        response = await self.http.get(f"{self.base_url}/api/v3/time")
        after = time.time() * 1000
        response.raise_for_status()
        server_time = response.json()['serverTime']
        if type(server_time) is not int or server_time <= 0:
            raise ValueError('Invalid MEXC server time')
        self.server_offset_ms = round(server_time - (before + after) / 2)

    async def wallet_list(self, path: str, params: dict | None = None) -> list[dict]:
        payload = await self._request("GET", path, params or {})
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise MexcAPIError(f"Некорректный список кошелька: {path}")
        return payload

    async def withdraw_usdt(self, amount: str, network: str, address: str,
                            memo: str, request_id: str) -> str:
        payload = await self._request("POST", "/api/v3/capital/withdraw", {
            "coin": "USDT", "netWork": network, "address": address,
            "memo": memo or None, "amount": amount, "withdrawOrderId": request_id})
        if not isinstance(payload, dict) or not payload.get("id"):
            raise MexcAPIError("Ответ на вывод не содержит ID; повтор запрещён, нужна сверка истории")
        return str(payload["id"])

    async def create_order(
        self,
        *,
        adv_no: str,
        amount: str | None = None,
        tradable_quantity: str | None = None,
        user_confirm_payment_id: int | None = None,
        user_confirm_pay_method_id: int | None = None,
    ) -> str:
        payload = await self._request(
            "POST",
            "/api/v3/fiat/merchant/order/deal",
            {
                "advNo": adv_no,
                "amount": amount,
                "tradableQuantity": tradable_quantity,
                "userConfirmPaymentId": user_confirm_payment_id,
                "userConfirmPayMethodId": user_confirm_pay_method_id,
            },
        )
        order_no = payload.get("data") if isinstance(payload, dict) else None
        if not order_no:
            raise MexcAPIError("Create order returned no advOrderNo")
        return str(order_no)

    async def mark_paid(self, order_no: str, payment_account_id: int) -> None:
        await self._request(
            "POST",
            "/api/v3/fiat/confirm_paid",
            {
                "advOrderNo": order_no,
                "userConfirmPaymentId": payment_account_id,
            },
        )

    async def release_coin(
        self,
        order_no: str,
        *,
        notify_type: str | None = None,
        notify_code: str | None = None,
    ) -> None:
        await self._request(
            "POST",
            "/api/v3/fiat/release_coin",
            {
                "advOrderNo": order_no,
                "notifyType": notify_type,
                "notifyCode": notify_code,
            },
        )

    async def generate_listen_key(self) -> str:
        payload = await self._request(
            "POST",
            "/api/v3/userDataStream",
            {},
        )
        # This endpoint may return the listenKey at top level.
        key = payload.get("listenKey") if isinstance(payload, dict) else None
        if not key and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            key = payload["data"].get("listenKey")
        if not key:
            raise MexcAPIError("listenKey not found in response")
        return str(key)

    async def get_conversation_id(self, order_no: str) -> int:
        payload = await self._request(
            "GET",
            "/api/v3/fiat/retrieveChatConversation",
            {"orderNo": order_no},
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        conversation_id = data.get("conversationId")
        if conversation_id is None:
            raise MexcAPIError("conversationId not found")
        return int(conversation_id)

    async def send_chat_text(self, order_no: str, text: str) -> None:
        listen_key = await self.generate_listen_key()
        conversation_id = await self.get_conversation_id(order_no)
        ws_url = (
            "wss://fiat.mexc.com/ws"
            f"?listenKey={listen_key}&conversationId={conversation_id}"
        )
        body = {
            "content": text,
            "conversationId": conversation_id,
            "type": 1,
            "imageUrl": "",
            "imageThumbUrl": "",
            "videoUrl": "",
            "fileUrl": "",
        }
        request = {
            "method": "SEND_MESSAGE",
            "params": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        }

        sock = None
        if self.proxy_url:
            try:
                # Some HTTP proxies answer CONNECT with HTTP/1.0, which the
                # websockets proxy parser rejects. python-socks accepts it.
                sock = await Proxy.from_url(self.proxy_url).connect(
                    dest_host='fiat.mexc.com', dest_port=443, timeout=15)
            except Exception as exc:
                raise MexcAPIError(f"Chat proxy connection failed ({type(exc).__name__})") from None
        try:
            async with websockets.connect(ws_url, open_timeout=15, close_timeout=5,
                                          proxy=None, **({'sock': sock} if sock else {})) as ws:
                await ws.send(json.dumps(request, ensure_ascii=False))
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                response = json.loads(raw)
                if not response.get("success"):
                    raise MexcAPIError(f"Chat send failed: {response}")
        finally:
            if sock is not None:
                sock.close()

    async def mark_chat_read(self, order_no: str) -> bool:
        # Disabled placeholder: fetching history is not a read receipt.
        # Add a verified endpoint or browser integration here later.
        return False
