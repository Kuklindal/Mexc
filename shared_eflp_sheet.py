"""Coordinate independent Eflp journals writing to one Google Sheets tab.

The lock is shared by processes on the same server. Column X gives every sale
a durable cycle ID, so a retry reuses its row after any partial write.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from journal import process_lock
from sheets import (EFLP_META_HEADER, EFLP_WEEKLY_MARKER, HEADER, SHARED_CYCLE_HEADER,
                    WEEK_SALES_FORMULA, WEEK_START_FORMULA, GoogleSheetsError,
                    _complete_sale_cycle, eflp_meta_values, eflp_weekly_formulas,
                    sale_values, sheet_number, week_choices)


@asynccontextmanager
async def _sheet_lock(sheet):
    """Wait for the other Eflp process without blocking this event loop."""
    for attempt in range(120):
        lock = process_lock(sheet.shared_lock)
        try:
            lock.__enter__()
        except RuntimeError:
            if attempt == 119:
                raise GoogleSheetsError('Общая вкладка Eflp занята другим ботом более 60 секунд') from None
            await asyncio.sleep(0.5)
            continue
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
        return


async def _rows(sheet):
    visible = (await sheet.request('GET', 'A:E')).get('values', [])
    metadata = (await sheet.request('GET', 'R:T')).get('values', [])
    cycle_ids = (await sheet.request('GET', 'X:X')).get('values', [])
    if visible and visible[0] != HEADER:
        raise GoogleSheetsError('Заголовки продаж отличаются от ожидаемых')
    if metadata and metadata[0] != EFLP_META_HEADER:
        raise GoogleSheetsError('Служебные заголовки Eflp отличаются от ожидаемых')
    if cycle_ids and cycle_ids[0] != SHARED_CYCLE_HEADER:
        raise GoogleSheetsError('Столбец X занят; для общей вкладки нужен ID цикла Eflp')
    return visible, metadata, cycle_ids


def _at(rows, index):
    return rows[index] if index < len(rows) else []


def _same_visible(row, sale):
    try:
        expected = sale_values(sale)
        return (len(row) == 5 and sheet_number(row[0]) == Decimal(str(expected[0]))
                and row[1:] == expected[1:])
    except (ValueError, TypeError, ArithmeticError):
        return False


def _positions(cycle_ids):
    positions = {}
    for row_number, row in enumerate(cycle_ids[1:], 2):
        if not row or not row[0]:
            continue
        if row[0] in positions:
            raise GoogleSheetsError('Один ID цикла записан в таблице дважды')
        positions[row[0]] = row_number
    return positions


def _external_sale(row_number, row, meta, cycle_id):
    if not meta or len(meta) != 3 or meta[1] not in {
            'eflp_volume', 'eflp_unique', 'eflp_volume_done', 'eflp_unique_done'}:
        return None
    if len(row) != 5 or not meta[0] or not meta[2]:
        raise GoogleSheetsError(f'Строка {row_number}: неполная продажа Eflp в общей вкладке')
    try:
        quantity = sheet_number(row[0])
        completed_at = datetime.strptime(row[1] + ' ' + row[2],
                                         '%d.%m.%Y %H:%M:%S').replace(
                                             tzinfo=timezone(timedelta(hours=7)))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        raise GoogleSheetsError(f'Строка {row_number}: неверные сумма или дата Eflp') from None
    return {'id': row_number - 1, 'cycle_id': 'external:' + (cycle_id or str(row_number)),
            'scheduler_mode': meta[1].removesuffix('_done'),
            'external_complete': meta[1].endswith('_done'),
            'p1_profile': meta[0], 'p1_nickname': row[4],
            'p2_profile': 'member:' + meta[2], 'p2_member_id': meta[2],
            'p2_nickname': row[3], 'quantity': str(quantity),
            'completed_at': completed_at.isoformat()}


async def prepare(sheet, journal):
    target = sheet.spreadsheet_id + '/' + sheet.tab
    if journal.sheet_target() not in {None, target}:
        raise GoogleSheetsError('Журнал привязан к другой Google Таблице/вкладке')
    async with _sheet_lock(sheet):
        visible, metadata, cycle_ids = await _rows(sheet)
        if not visible:
            await sheet.request('PUT', 'A1:E1', [HEADER])
        if not metadata:
            await sheet.request('PUT', 'R1:T1', [EFLP_META_HEADER])
        if not cycle_ids:
            await sheet.request('PUT', 'X1', [SHARED_CYCLE_HEADER])
        positions = _positions(cycle_ids)
        # Existing single-writer Eflp sales used sale ID + 1 as their row.
        # Claim only rows that match this journal exactly; never claim another
        # process's row by position alone.
        legacy_sales = journal.sales() if journal.sheet_target() == target else []
        for sale in legacy_sales:
            if sale['cycle_id'] in positions:
                continue
            row_number = int(sale['id']) + 1
            row = _at(visible, row_number - 1)
            meta = _at(metadata, row_number - 1)
            claimed = _at(cycle_ids, row_number - 1)
            if (not claimed and _same_visible(row, sale)
                    and meta in (eflp_meta_values(sale),
                                 eflp_meta_values(sale, completed=_complete_sale_cycle(journal, sale)))):
                await sheet.request('PUT', f'X{row_number}', [[sale['cycle_id']]])
                positions[sale['cycle_id']] = row_number
            elif sale['sent']:
                raise GoogleSheetsError(f'Продажа {sale["id"]} не найдена в общей вкладке; '
                                        'сверьте таблицу до запуска второго бота')
        journal.bind_sheet(target)
        sheet.ready = True


async def send(sheet, sale):
    async with _sheet_lock(sheet):
        visible, metadata, cycle_ids = await _rows(sheet)
        positions = _positions(cycle_ids)
        row_number = positions.get(sale['cycle_id'])
        if row_number is None:
            row_number = max(len(visible), len(metadata), len(cycle_ids), 1) + 1
            # Claim first. If the process dies after this request, a retry
            # completes the same row rather than appending a duplicate sale.
            await sheet.request('PUT', f'X{row_number}', [[sale['cycle_id']]])
        row = _at(visible, row_number - 1)
        meta = _at(metadata, row_number - 1)
        expected_meta = eflp_meta_values(sale)
        if row and not _same_visible(row, sale):
            raise GoogleSheetsError(f'Строка {row_number} отличается от продажи журнала')
        if meta and meta not in (expected_meta, eflp_meta_values(sale, completed=True)):
            raise GoogleSheetsError(f'Служебные поля строки {row_number} отличаются от журнала')
        if not row:
            await sheet.request('PUT', f'A{row_number}:E{row_number}', [sale_values(sale)])
        if not meta:
            await sheet.request('PUT', f'R{row_number}:T{row_number}', [expected_meta])


async def read_sales(sheet, journal):
    if not sheet.ready:
        await prepare(sheet, journal)
    target = sheet.spreadsheet_id + '/' + sheet.tab
    if journal.sheet_target() not in {None, target}:
        raise GoogleSheetsError('Журнал привязан к другой Google Таблице/вкладке')
    async with _sheet_lock(sheet):
        visible, metadata, cycle_ids = await _rows(sheet)
        positions = _positions(cycle_ids)
        local_sales = journal.sales()
        own_ids = {sale['cycle_id'] for sale in local_sales}
        confirmed = []
        for sale in local_sales:
            row_number = positions.get(sale['cycle_id'])
            if row_number is None:
                if sale['sent']:
                    raise GoogleSheetsError('Доставленная продажа отсутствует в общей вкладке')
                confirmed.append(sale)
                continue
            row = _at(visible, row_number - 1)
            meta = _at(metadata, row_number - 1)
            if (not _same_visible(row, sale)
                    or meta not in (eflp_meta_values(sale),
                                    eflp_meta_values(sale, completed=_complete_sale_cycle(journal, sale)))):
                raise GoogleSheetsError(f'Строка {row_number} общей вкладки отличается от журнала')
            confirmed.append(sale)
        for row_number in range(2, max(len(visible), len(metadata), len(cycle_ids)) + 1):
            cycle_id = (_at(cycle_ids, row_number - 1) or [''])[0]
            if cycle_id in own_ids:
                continue
            external = _external_sale(row_number, _at(visible, row_number - 1),
                                      _at(metadata, row_number - 1), cycle_id)
            if external:
                confirmed.append(external)
        return confirmed


def _cell(value):
    if not value:
        return {}
    return {'userEnteredValue': {'formulaValue' if value.startswith('=') else 'stringValue': value}}


async def send_weekly(sheet, sales, journal):
    if journal is None:
        raise GoogleSheetsError('Общая вкладка Eflp требует журнал для сверки завершённых циклов')
    async with _sheet_lock(sheet):
        visible, metadata, cycle_ids = await _rows(sheet)
        positions = _positions(cycle_ids)
        for sale in sales:
            row_number = positions.get(sale['cycle_id'])
            if row_number is None:
                if sale['sent']:
                    raise GoogleSheetsError('Продажа журнала отсутствует в общей вкладке')
                continue
            if not _same_visible(_at(visible, row_number - 1), sale):
                raise GoogleSheetsError(f'Строка {row_number} общей вкладки отличается от журнала')
            current = _at(metadata, row_number - 1)
            expected = eflp_meta_values(sale, completed=_complete_sale_cycle(journal, sale))
            if current not in (eflp_meta_values(sale), expected, []):
                raise GoogleSheetsError(f'Служебные поля строки {row_number} отличаются от журнала')
            if current != expected:
                await sheet.request('PUT', f'R{row_number}:T{row_number}', [expected])
                while len(metadata) < row_number:
                    metadata.append([])
                metadata[row_number - 1] = expected

        all_sales = []
        for row_number in range(2, max(len(visible), len(metadata)) + 1):
            item = _external_sale(row_number, _at(visible, row_number - 1),
                                  _at(metadata, row_number - 1),
                                  (_at(cycle_ids, row_number - 1) or [''])[0])
            if item:
                all_sales.append(item)
        existing = (await sheet.request('GET', 'F:J')).get('values', [])
        if existing and any(any(value != '' for value in row) for row in existing):
            if existing[0][0] != EFLP_WEEKLY_MARKER:
                raise GoogleSheetsError('Столбцы F:J заняты другой сводкой')
        selected, choices = week_choices(all_sales,
            existing[0][1] if existing and len(existing[0]) > 1 else None)
        rows = eflp_weekly_formulas(all_sales, selected, len(existing))
        existing_dates = (await sheet.request('GET', 'U1:U2',
            value_render_option='FORMULA')).get('values', [])
        if existing_dates and existing_dates != [[WEEK_START_FORMULA], [WEEK_SALES_FORMULA]]:
            raise GoogleSheetsError('Столбец U занят; служебные даты отличаются')
        sheet_id = await sheet.get_sheet_id()
        requests = [
            {'updateCells': {'range': {'sheetId': sheet_id, 'startRowIndex': 0,
                'endRowIndex': 2, 'startColumnIndex': 20, 'endColumnIndex': 21},
                'rows': [{'values': [_cell(WEEK_START_FORMULA)]},
                         {'values': [_cell(WEEK_SALES_FORMULA)]}], 'fields': 'userEnteredValue'}},
            {'updateCells': {'range': {'sheetId': sheet_id, 'startRowIndex': 0,
                'endRowIndex': len(rows), 'startColumnIndex': 5, 'endColumnIndex': 10},
                'rows': [{'values': [_cell(value) for value in row]} for row in rows],
                'fields': 'userEnteredValue'}},
            {'setDataValidation': {'range': {'sheetId': sheet_id, 'startRowIndex': 0,
                'endRowIndex': 1, 'startColumnIndex': 6, 'endColumnIndex': 7},
                'rule': {'condition': {'type': 'ONE_OF_LIST',
                    'values': [{'userEnteredValue': choice} for choice in choices]},
                    'strict': True, 'showCustomUi': True,
                    'inputMessage': 'Выберите неделю: четверг 18:50 МСК'}}},
        ]
        await sheet.batch_update(requests)
