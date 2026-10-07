"""Google Sheets export to a dedicated tab, with retry-stable row addresses."""
from __future__ import annotations

import asyncio
from functools import partial
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import os
from urllib.parse import quote

import httpx

from journal import Journal
from notifier import TelegramNotifier
from config import p2_nickname


HEADER = ["Сумма продажи (USDT)", "Дата (Красноярск)", "Время (UTC+7)",
          "Профиль П2", "Профиль П1"]
WEEKLY_MARKER = "Недельная сводка MEXC"
EFLP_WEEKLY_MARKER = "Недельная сводка Eflp"
EFLP_MARKER = "П1"
OLD_EFLP_MARKER = "Eflp: аккаунт П1"
OLD_EFLP_META_HEADER = ["П1 для сводки Eflp", "Режим", "MEMBER_ID П2"]
EFLP_META_HEADER = ["Ключ П1 для сводки Eflp", "Режим", "MEMBER_ID П2"]
WEEK_START_FORMULA = '=IFERROR(DATEVALUE(LEFT($G$1;10))+TIMEVALUE(RIGHT($G$1;5));0)'
WEEK_SALES_FORMULA = '=ARRAYFORMULA(IF(B2:B="";"";IFERROR(DATEVALUE(B2:B)+TIMEVALUE(C2:C)-4/24;"")))'
MOSCOW = timezone(timedelta(hours=3))


def weekly_start(moment: datetime) -> datetime:
    local = moment.astimezone(MOSCOW)
    start = (local - timedelta(days=(local.weekday() - 3) % 7)).replace(
        hour=18, minute=50, second=0, microsecond=0)
    return start if local >= start else start - timedelta(days=7)


def week_label(start: datetime) -> str:
    return start.strftime("%d.%m.%Y %H:%M")


def week_choices(sales: list[dict], selected: str | None = None,
                 moment: datetime | None = None) -> tuple[str, list[str]]:
    current = weekly_start(moment or datetime.now(timezone.utc))
    starts = [weekly_start(datetime.fromisoformat(sale["completed_at"])) for sale in sales]
    first = min(starts + [current])
    last = max(starts + [current]) + timedelta(days=7)
    choices = [week_label(first + timedelta(days=7 * i))
               for i in range((last - first).days // 7 + 1)]
    try:
        parsed = datetime.strptime((selected or "").removesuffix(" МСК"), "%d.%m.%Y %H:%M")
        chosen = week_label(weekly_start(parsed.replace(tzinfo=MOSCOW)))
    except ValueError:
        chosen = week_label(current)
    if chosen not in choices:
        choices.append(chosen)
    return chosen, list(reversed(choices))


def weekly_formulas(sales: list[dict], selected: str, row_count: int) -> list[list[str]]:
    participants = [name.strip() for name in os.getenv("WEEKLY_PARTICIPANTS", "VERS,Danil,Lenya").split(",")]
    if len(participants) != 3 or any(not name for name in participants):
        raise GoogleSheetsError("WEEKLY_PARTICIPANTS: укажите ровно три имени через запятую")
    profiles = sorted({sale_profile(sale) for sale in sales})
    rows = [["" for _ in range(5)] for _ in range(max(18, 11 + len(profiles), row_count))]
    rows[0] = [WEEKLY_MARKER, selected, "До (не включительно)",
               '=TEXT($U$1+7;"dd.mm.yyyy hh:mm")&" МСК"', ""]
    rows[2] = ["Общий объем USDT",
                '=SUMIFS($A$2:$A;$U$2:$U;">="&$U$1;$U$2:$U;"<"&($U$1+7))',
               "Придёт USD",
               '=IFS(G3<1000000;0;G3<2000000;300;G3<2500000;450;TRUE;525)', ""]
    rows[3] = ["В общаг USD", '=I3*5%', "", "", ""]
    rows[5] = ["Недельный итог без премии", "Объем USDT", "ЗП USD", "Премия USD", "Итого USD"]
    for i, name in enumerate(participants, 7):
        rows[i - 1] = [name,
                       '=ROUND($G$3/3;4)' if i < 9 else '=$G$3-SUM(G7:G8)',
                       '=ROUND($I$3*95%/3;2)' if i < 9 else '=$I$3*95%-SUM(H7:H8)',
                       '=ROUND(200/3;2)' if i < 9 else '=200-SUM(I7:I8)',
                       f'=H{i}+I{i}']
    rows[10] = ["Продажи по профилям П2", "USDT", "", "", ""]
    for i, profile in enumerate(profiles, 12):
        rows[i - 1] = [profile,
                       f'=SUMIFS($A$2:$A;$D$2:$D;F{i};$U$2:$U;">="&$U$1;$U$2:$U;"<"&($U$1+7))',
                       "", "", ""]
    return rows

def krasnoyarsk_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=7))).strftime("%d.%m.%Y %H:%M:%S")


def sale_values(sale: dict) -> list:
    quantity = Decimal(sale.get("quantity") or "NaN")
    if not quantity.is_finite() or quantity <= 0 or not sale.get("completed_at"):
        raise GoogleSheetsError("В журнале нет количества USDT или времени первой продажи; запись остановлена")
    date, clock = krasnoyarsk_time(sale["completed_at"]).split()
    return [float(quantity), date, clock, sale_profile(sale), sale_p1_name(sale)]


def sale_profile(sale: dict) -> str:
    return p2_nickname(sale.get("p2_profile") or "default", sale.get("p2_nickname"))


def sale_p1_name(sale: dict) -> str:
    profile = sale.get('p1_profile') or 'p1'
    saved = (sale.get('p1_nickname') or '').strip()
    if saved:
        return saved
    return os.getenv(f'MEXC_{profile.upper()}_NICKNAME', '').strip() or profile


def eflp_meta_values(sale: dict) -> list[str]:
    return [sale.get('p1_profile') or 'p1', sale.get('scheduler_mode') or '',
            sale.get('p2_member_id') or '']


def eflp_formulas(sales: list[dict], row_count: int, *, first_data_row: int = 2,
                  unique_col: str = 'O', sales_col: str = 'P') -> list[list[str]]:
    accounts = {sale.get('p1_profile') or 'p1': sale_p1_name(sale) for sale in sales
                if sale.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}}
    rows = [['', '', '', ''] for _ in range(max(2, row_count, len(accounts) + 1))]
    rows[0] = [EFLP_MARKER, 'Уникальных П2', 'Общая сумма продаж (USDT)', 'Итого USDT']
    for row_number, key in enumerate(sorted(accounts), first_data_row):
        rows[row_number - first_data_row + 1] = [accounts[key],
            f'=COUNTUNIQUEIFS($T$2:$T;$R$2:$R;"{key}";$S$2:$S;"eflp*";$U$2:$U;">="&$U$1;$U$2:$U;"<"&($U$1+7);$T$2:$T;"<>")',
            f'=SUMIFS($A$2:$A;$R$2:$R;"{key}";$S$2:$S;"eflp*";$U$2:$U;">="&$U$1;$U$2:$U;"<"&($U$1+7))',
            f'=IF(AND({unique_col}{row_number}>=20;{sales_col}{row_number}>20000);90;0)']
    return rows


def eflp_weekly_formulas(sales: list[dict], selected: str, row_count: int) -> list[list[str]]:
    table = eflp_formulas(sales, max(2, row_count - 1), first_data_row=3,
                          unique_col='G', sales_col='H')
    rows = [[EFLP_WEEKLY_MARKER, selected, 'До (не включительно)',
             '=TEXT($U$1+7;"dd.mm.yyyy hh:mm")&" МСК"', '']]
    rows.extend([row + [''] for row in table])
    return rows


def sheet_number(value) -> Decimal:
    # Google returns formatted values according to the spreadsheet locale.
    return Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))


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
        self.sheet_id = None

    async def token(self) -> str:
        if not self.credentials.valid:
            from google.auth.transport.requests import Request
            await asyncio.to_thread(self.credentials.refresh, partial(Request(), timeout=20))
        return self.credentials.token

    async def request(self, method: str, cell_range: str, values: list | None = None,
                      *, value_render_option: str | None = None) -> dict:
        token = await self.token()
        a1 = "'" + self.tab.replace("'", "''") + "'!" + cell_range
        url = ("https://sheets.googleapis.com/v4/spreadsheets/"
               + quote(self.spreadsheet_id, safe="") + "/values/" + quote(a1, safe=""))
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, url,
                headers={"Authorization": f"Bearer {token}"},
                params=({"valueInputOption": "RAW"} if values is not None else
                        {"valueRenderOption": value_render_option} if value_render_option else None),
                json={"values": values} if values is not None else None)
        if response.status_code >= 400:
            hints = {403: ("запись отклонена для сервисного аккаунта в этой таблице; проверьте его роль для GOOGLE_SHEET_ID и ограничения файла"
                           if method != "GET" else "нет доступа на чтение таблицы для сервисного аккаунта"),
                     404: "проверьте GOOGLE_SHEET_ID и доступ сервисного аккаунта",
                     400: "проверьте GOOGLE_SHEET_TAB: вкладка с таким названием должна существовать"}
            raise GoogleSheetsError(f"Google Sheets HTTP {response.status_code}; "
                                    + hints.get(response.status_code, "проверьте настройки или повторите позже"))
        return response.json()

    async def batch_update(self, requests: list[dict]) -> dict:
        token = await self.token()
        url = ("https://sheets.googleapis.com/v4/spreadsheets/"
               + quote(self.spreadsheet_id, safe="") + ":batchUpdate")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"},
                                         json={"requests": requests})
        if response.status_code >= 400:
            raise GoogleSheetsError(f"Google Sheets batchUpdate HTTP {response.status_code}; проверьте права и формулы")
        return response.json()

    async def get_sheet_id(self) -> int:
        if self.sheet_id is not None:
            return self.sheet_id
        token = await self.token()
        url = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(self.spreadsheet_id, safe="")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"},
                                        params={"fields": "sheets(properties(sheetId,title))"})
        if response.status_code >= 400:
            raise GoogleSheetsError(f"Google Sheets metadata HTTP {response.status_code}")
        matches = [sheet["properties"]["sheetId"] for sheet in response.json().get("sheets", [])
                   if sheet.get("properties", {}).get("title") == self.tab]
        if len(matches) != 1:
            raise GoogleSheetsError("Вкладка Google Таблицы не найдена однозначно")
        self.sheet_id = matches[0]
        return self.sheet_id

    async def prepare(self, journal: Journal):
        if self.ready:
            return
        target = self.spreadsheet_id + "/" + self.tab
        existing = (await self.request("GET", "A:D")).get("values", [])
        p1_column = (await self.request("GET", "E:E")).get("values", [])
        if journal.sheet_target() is None and existing:
            raise GoogleSheetsError("Для первого подключения нужна пустая отдельная вкладка A:D")
        legacy = bool(existing and existing[0] == ["Сумма продажи"])
        old_header = bool(existing and existing[0] == HEADER[:3])
        four_column = bool(existing and existing[0] == HEADER[:4])
        if existing and not (four_column or legacy or old_header):
            raise GoogleSheetsError("Заголовок таблицы изменён; запись остановлена")
        sales = {int(s['id']): s for s in journal.sales()}
        has_p1_column = bool(p1_column and p1_column[0] == [HEADER[4]])
        old_week_array = False
        if has_p1_column:
            for i, row in enumerate(p1_column[1:], 1):
                if row and (i not in sales or row != [sale_p1_name(sales[i])]):
                    raise GoogleSheetsError("Столбец П1 отличается от журнала; запись остановлена")
        elif p1_column:
            # E2 was an array formula, so old helper dates can fill every sale row.
            formulas = (await self.request("GET", "E1:E2", value_render_option="FORMULA")).get("values", [])
            summary = (await self.request("GET", "F:J")).get("values", [])
            old_week_array = formulas == [[WEEK_START_FORMULA], [WEEK_SALES_FORMULA]]
            interrupted_migration = (formulas == [[WEEK_START_FORMULA]]
                                     and not any(row for row in p1_column[2:]))
            if (not (old_week_array or interrupted_migration)
                    or not summary or summary[0][0] not in {WEEKLY_MARKER, EFLP_WEEKLY_MARKER}):
                raise GoogleSheetsError("Столбец E занят; профиль П1 не записан")
        journal.bind_sheet(target)
        if old_week_array:
            # Clear the array-formula anchor before replacing its spill with P1
            # values. E1 remains as a marker if this migration is interrupted.
            await self.batch_update([{"updateCells": {
                "range": {"sheetId": await self.get_sheet_id(), "startRowIndex": 1,
                          "endRowIndex": 2, "startColumnIndex": 4, "endColumnIndex": 5},
                "rows": [{"values": [{}]}], "fields": "userEnteredValue"}}])
        if legacy or old_header:
            for i, row in enumerate(existing[1:], 1):
                if not row:
                    continue
                expected = (Decimal(sales[i]["amount"]) if legacy else sale_values(sales[i])[:3]) if i in sales else None
                try:
                    matches = (len(row) == 1 and sheet_number(row[0]) == expected) if legacy else (
                        len(row) == 3 and sheet_number(row[0]) == Decimal(str(expected[0]))
                        and row[1:] == expected[1:])
                except (ValueError, ArithmeticError, TypeError):
                    matches = False
                if expected is None or not matches:
                    raise GoogleSheetsError("Старые строки отличаются от журнала; автоматическая замена рублей на USDT остановлена")
            rows = [HEADER] + [sale_values(sales[i]) if i in sales else [] for i in range(1, max(sales, default=0) + 1)]
            await self.request("PUT", f"A1:E{len(rows)}", rows)
        else:
            # Existing journals used the internal profile key in column D. Change
            # only that column after checking A:C against our own sale records.
            change_names = False
            names = []
            for i, row in enumerate(existing[1:], 1):
                sale = sales.get(i)
                if not row:
                    names.append([])
                    continue
                if sale is None or len(row) != 4:
                    raise GoogleSheetsError("Строки Google Таблицы отличаются от журнала; имена профилей не обновлены")
                expected = sale_values(sale)
                try:
                    matches = (sheet_number(row[0]) == Decimal(str(expected[0]))
                               and row[1:3] == expected[1:3]
                               and row[3] in {expected[3], sale['p2_profile']})
                except (ValueError, ArithmeticError, TypeError):
                    matches = False
                if not matches:
                    raise GoogleSheetsError("Строки Google Таблицы отличаются от журнала; имена профилей не обновлены")
                change_names |= row[3] != expected[3]
                names.append([expected[3]])
            if change_names:
                await self.request("PUT", f"D2:D{len(names) + 1}", names)
            p1_rows = [[HEADER[4]]] + [[sale_p1_name(sales[i])] if i in sales else []
                                       for i in range(1, max(sales, default=0) + 1)]
            await self.request("PUT", f"E1:E{len(p1_rows)}", p1_rows)
            await self.request("PUT", "A1:D1", [HEADER[:4]])
        self.ready = True

    async def send(self, sale: dict):
        row = int(sale["id"]) + 1
        values = [sale_values(sale)]
        # A timeout after a successful write can safely be retried at the same row.
        await self.request("PUT", f"A{row}:E{row}", values)

    async def send_weekly(self, sales: list[dict]):
        eflp = (any(sale.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'} for sale in sales)
                or bool(os.getenv('EFLP_P1_PROFILES', '').strip()))
        existing = (await self.request("GET", "F:J")).get("values", [])
        occupied = any(any(cell != "" for cell in row) for row in existing)
        if occupied and (
                not existing[0] or existing[0][0] not in
                ({WEEKLY_MARKER, EFLP_WEEKLY_MARKER} if eflp else {WEEKLY_MARKER})):
            raise GoogleSheetsError("Столбцы F:J заняты; недельная сводка не перезаписала чужие данные")
        selected, choices = week_choices(sales, existing[0][1] if existing and len(existing[0]) > 1 else None)
        rows = (eflp_weekly_formulas(sales, selected, len(existing)) if eflp else
                weekly_formulas(sales, selected, len(existing)))
        eflp_existing = (await self.request('GET', 'N:Q')).get('values', [])
        old_eflp_header = [OLD_EFLP_MARKER, 'Объём USDT', 'Уникальных П2']
        new_eflp_header = [EFLP_MARKER, 'Уникальных П2', 'Общая сумма продаж (USDT)', 'Итого USDT']
        old_layout = bool(eflp_existing and eflp_existing[0][:3] == old_eflp_header)
        new_layout = bool(eflp_existing and eflp_existing[0][:4] == new_eflp_header)
        if any(any(cell != '' for cell in row) for row in eflp_existing) and not (old_layout or new_layout):
            raise GoogleSheetsError('Столбцы N:Q заняты; сводка Eflp не перезаписала чужие данные')
        existing_dates = (await self.request('GET', 'U1:U2',
                                             value_render_option='FORMULA')).get('values', [])
        if existing_dates and existing_dates != [[WEEK_START_FORMULA], [WEEK_SALES_FORMULA]]:
            raise GoogleSheetsError('Столбец U занят; служебные даты не записаны')
        eflp_rows = ([['', '', '', ''] for _ in range(max(1, len(eflp_existing)))] if eflp else
                     eflp_formulas(sales, len(eflp_existing)))
        by_row = {int(sale['id']): sale for sale in sales}
        metadata = [EFLP_META_HEADER] + [
            eflp_meta_values(by_row[row]) if row in by_row else []
            for row in range(1, max(by_row, default=0) + 1)]
        existing_meta = (await self.request('GET', 'Q:S' if old_layout else 'R:T')).get('values', [])
        if old_layout and any((await self.request('GET', 'T:T')).get('values', [])):
            raise GoogleSheetsError('Столбец T занят; данные Eflp не перенесены')
        expected_header = OLD_EFLP_META_HEADER if old_layout else EFLP_META_HEADER
        if existing_meta and existing_meta[0] != expected_header:
            raise GoogleSheetsError('Служебные столбцы Eflp заняты; запись остановлена')
        for index, row in enumerate(existing_meta[1:], 1):
            expected = ([sale_p1_name(by_row[index]), by_row[index].get('scheduler_mode') or '',
                         by_row[index].get('p2_member_id') or '']
                        if old_layout and index in by_row else
                        metadata[index] if index < len(metadata) else [])
            if row and (row + [''] * (3 - len(row))) != expected:
                raise GoogleSheetsError('Служебные строки Eflp расходятся с журналом; запись остановлена')
        metadata.extend([] for _ in range(max(0, len(existing_meta) - len(metadata))))
        sheet_id = await self.get_sheet_id()

        def cell(value):
            if not value:
                return {}
            return {"userEnteredValue": {"formulaValue" if value.startswith("=") else "stringValue": value}}

        requests = [
            {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                         "endRowIndex": 2, "startColumnIndex": 20, "endColumnIndex": 21},
                              "rows": [{"values": [cell(WEEK_START_FORMULA)]},
                                       {"values": [cell(WEEK_SALES_FORMULA)]}
                                      ], "fields": "userEnteredValue"}},
            {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                         "endRowIndex": len(rows), "startColumnIndex": 5, "endColumnIndex": 10},
                             "rows": [{"values": [cell(value) for value in row]} for row in rows],
                             "fields": "userEnteredValue"}},
            {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                         "endRowIndex": len(eflp_rows), "startColumnIndex": 13, "endColumnIndex": 17},
                              "rows": [{"values": [cell(value) for value in row]} for row in eflp_rows],
                              "fields": "userEnteredValue"}},
            {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                         "endRowIndex": len(metadata), "startColumnIndex": 17, "endColumnIndex": 20},
                              "rows": [{"values": [cell(value) for value in row]} for row in metadata],
                              "fields": "userEnteredValue"}},
            {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                             "endRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 7},
                                   "rule": {"condition": {"type": "ONE_OF_LIST",
                                                          "values": [{"userEnteredValue": choice} for choice in choices]},
                                            "strict": True, "showCustomUi": True,
                                            "inputMessage": "Выберите неделю: четверг 18:50 МСК"}}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                      "startIndex": 4, "endIndex": 5},
                                            "properties": {"hiddenByUser": False},
                                            "fields": "hiddenByUser"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                      "startIndex": 13, "endIndex": 17},
                                            "properties": {"hiddenByUser": eflp},
                                            "fields": "hiddenByUser"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                      "startIndex": 17, "endIndex": 21},
                                            "properties": {"hiddenByUser": True},
                                            "fields": "hiddenByUser"}},
        ]
        await self.batch_update(requests)


class Reporter:
    def __init__(self, journal: Journal, telegram: TelegramNotifier, sheets: GoogleSheets | None,
                 keyboard=None):
        self.journal = journal
        self.telegram = telegram
        self.sheets = sheets
        self.keyboard = keyboard
        self.logger = logging.getLogger("mexc_p2p.journal")
        self.sheet_error_reported = False

    def telegram_text(self, event: dict) -> str:
        if event['step'] in {'rollover_switch_notice', 'ad_rejection_alert',
                             'eflp_profile_done', 'eflp_mode_done'}:
            return event['message']
        cid = event["cycle_id"]
        cycle = self.journal.cycle(cid)
        spec = cycle['spec'] if cycle else {}
        profile = p2_nickname(spec.get('p2_profile', 'default'), spec.get('nicknames', {}).get('p2'))
        if event["step"] == "google_sheets":
            return (f"⚠️ Google Таблица: запись отложена\nЦикл: {cid}\nП2: {profile}\n{event['message']}\n"
                    "Продажа сохранена в журнале. После исправления: python main.py sync-journal")
        return (f"❌ Цикл {cid} остановлен\nП2: {profile}\nШаг: {event['step']} | Участник: {event['actor']}\n"
                f"{event['message']}\nОрдер: {event['order_no'] or '—'}\n"
                f"Сумма: {event['amount'] or '—'} {event['fiat']} | {event['quantity'] or '—'} USDT\n"
                f"Красноярск: {krasnoyarsk_time(event['time'])}\n"
                + ("Цикл сброшен; ордера на MEXC не отменены." if event["status"] == "abandoned" else
                   f"Продолжение: python main.py cycle --resume {cid}"))

    async def flush(self, *, force_sheets: bool = False) -> dict[str, int]:
        sales = self.journal.pending_sales()
        if self.sheets and (sales or force_sheets):
            try:
                await self.sheets.prepare(self.journal)
                for sale in sales:
                    await self.sheets.send(sale)
                await self.sheets.send_weekly(self.journal.sales())
                for sale in sales:
                    self.journal.sale_delivered(sale["id"])
                self.sheet_error_reported = False
            except Exception as exc:
                if not sales:
                    raise
                reason = str(exc) if isinstance(exc, GoogleSheetsError) else type(exc).__name__
                self.logger.warning("Google Sheets: доставка отложена (%s). Выполните sync-journal после исправления настроек.", reason)
                if not self.sheet_error_reported:
                    self.journal.transition(sales[0]["cycle_id"], "google_sheets", "system", "error", reason)
                    self.sheet_error_reported = True
        if self.telegram.enabled:
            for event in self.journal.pending("telegram"):
                important = (event["status"] in {"error", "paused"}
                              or event['step'] in {'rollover_switch_notice', 'ad_rejection_alert',
                                                   'eflp_profile_done', 'eflp_mode_done'})
                if not important:
                    # Suppress old queued progress messages too; retain the full local history.
                    self.journal.delivered("telegram", event["id"])
                    continue
                text = self.telegram_text(event)
                sent = (await self.telegram.send(text, reply_markup=self.keyboard()) if self.keyboard
                        else await self.telegram.send(text))
                if not sent:
                    break
                self.journal.delivered("telegram", event["id"])
        return {"telegram": len(self.journal.pending("telegram")), "sales": len(self.journal.pending_sales())}
