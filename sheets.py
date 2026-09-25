"""Google Sheets export to a dedicated tab, with retry-stable row addresses."""
from __future__ import annotations

import asyncio
from functools import partial
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from urllib.parse import quote

import httpx

from journal import Journal
from notifier import TelegramNotifier


HEADER = ["Сумма продажи (USDT)", "Дата (Красноярск)", "Время (UTC+7)"]

def krasnoyarsk_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=7))).strftime("%d.%m.%Y %H:%M:%S")


def sale_values(sale: dict) -> list:
    quantity = Decimal(sale.get("quantity") or "NaN")
    if not quantity.is_finite() or quantity <= 0 or not sale.get("completed_at"):
        raise GoogleSheetsError("В журнале нет количества USDT или времени первой продажи; запись остановлена")
    date, clock = krasnoyarsk_time(sale["completed_at"]).split()
    return [float(quantity), date, clock]


class GoogleSheetsError(RuntimeError):
    """Safe diagnostic text without credentials or raw HTTP responses."""


class GoogleSheets:
    def __init__(self, spreadsheet_id: str, tab: str, credentials_file: str):
        from google.oauth2.service_account import Credentials
        self.credentials = Credentials.from_service_account_file(
            credentials_file, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.spreadsheet_id = spreadsheet_id
        self.tab = tab
        self.ready = False

    async def request(self, method: str, cell_range: str, values: list | None = None) -> dict:
        if not self.credentials.valid:
            from google.auth.transport.requests import Request
            await asyncio.to_thread(self.credentials.refresh, partial(Request(), timeout=20))
        a1 = "'" + self.tab.replace("'", "''") + "'!" + cell_range
        url = ("https://sheets.googleapis.com/v4/spreadsheets/"
               + quote(self.spreadsheet_id, safe="") + "/values/" + quote(a1, safe=""))
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, url,
                headers={"Authorization": f"Bearer {self.credentials.token}"},
                params={"valueInputOption": "RAW"} if values is not None else None,
                json={"values": values} if values is not None else None)
        if response.status_code >= 400:
            hints = {403: "дайте сервисному аккаунту доступ Редактор к таблице и проверьте включение Google Sheets API",
                     404: "проверьте GOOGLE_SHEET_ID и доступ сервисного аккаунта",
                     400: "проверьте GOOGLE_SHEET_TAB: вкладка с таким названием должна существовать"}
            raise GoogleSheetsError(f"Google Sheets HTTP {response.status_code}; "
                                    + hints.get(response.status_code, "проверьте настройки или повторите позже"))
        return response.json()

    async def prepare(self, journal: Journal):
        if self.ready:
            return
        target = self.spreadsheet_id + "/" + self.tab
        existing = (await self.request("GET", "A:C")).get("values", [])
        if journal.sheet_target() is None and existing:
            raise GoogleSheetsError("Для первого подключения нужна пустая отдельная вкладка A:C")
        legacy = bool(existing and existing[0] == ["Сумма продажи"])
        if existing and existing[0] != HEADER and not legacy:
            raise GoogleSheetsError("Заголовок таблицы изменён; запись остановлена")
        journal.bind_sheet(target)
        if legacy:
            sales = {int(s['id']): s for s in journal.sales()}
            for i, row in enumerate(existing[1:], 1):
                if not row:
                    continue
                if (i not in sales or len(row) != 1 or
                        Decimal(str(row[0])) != Decimal(sales[i]["amount"])):
                    raise GoogleSheetsError("Старые строки отличаются от журнала; автоматическая замена рублей на USDT остановлена")
            rows = [HEADER] + [sale_values(sales[i]) if i in sales else [] for i in range(1, max(sales, default=0) + 1)]
            await self.request("PUT", f"A1:C{len(rows)}", rows)
        else:
            await self.request("PUT", "A1:C1", [HEADER])
        self.ready = True

    async def send(self, sale: dict):
        row = int(sale["id"]) + 1
        values = [sale_values(sale)]
        # A timeout after a successful write can safely be retried at the same row.
        await self.request("PUT", f"A{row}:C{row}", values)


class Reporter:
    def __init__(self, journal: Journal, telegram: TelegramNotifier, sheets: GoogleSheets | None):
        self.journal = journal
        self.telegram = telegram
        self.sheets = sheets
        self.logger = logging.getLogger("mexc_p2p.journal")
        self.sheet_error_reported = False

    def telegram_text(self, event: dict) -> str:
        cid = event["cycle_id"]
        if event["step"] == "google_sheets":
            return (f"⚠️ Google Таблица: запись отложена\nЦикл: {cid}\n{event['message']}\n"
                    "Продажа сохранена в журнале. После исправления: python main.py sync-journal")
        return (f"❌ Цикл {cid} остановлен\nШаг: {event['step']} | Участник: {event['actor']}\n"
                f"{event['message']}\nОрдер: {event['order_no'] or '—'}\n"
                f"Сумма: {event['amount'] or '—'} {event['fiat']} | {event['quantity'] or '—'} USDT\n"
                f"Красноярск: {krasnoyarsk_time(event['time'])}\n"
                + ("Цикл сброшен; ордера на MEXC не отменены." if event["status"] == "abandoned" else
                   f"Продолжение: python main.py cycle --resume {cid}"))

    async def flush(self) -> dict[str, int]:
        if self.sheets and self.journal.pending_sales():
            sales = self.journal.pending_sales()
            try:
                await self.sheets.prepare(self.journal)
                for sale in sales:
                    await self.sheets.send(sale)
                    self.journal.sale_delivered(sale["id"])
                self.sheet_error_reported = False
            except Exception as exc:
                reason = str(exc) if isinstance(exc, GoogleSheetsError) else type(exc).__name__
                self.logger.warning("Google Sheets: доставка отложена (%s). Выполните sync-journal после исправления настроек.", reason)
                if not self.sheet_error_reported:
                    self.journal.transition(sales[0]["cycle_id"], "google_sheets", "system", "error", reason)
                    self.sheet_error_reported = True
        if self.telegram.enabled:
            for event in self.journal.pending("telegram"):
                important = event["status"] in {"error", "paused"}
                if not important:
                    # Suppress old queued progress messages too; retain the full local history.
                    self.journal.delivered("telegram", event["id"])
                    continue
                text = self.telegram_text(event)
                if not await self.telegram.send(text):
                    break
                self.journal.delivered("telegram", event["id"])
        return {"telegram": len(self.journal.pending("telegram")), "sales": len(self.journal.pending_sales())}
