import tempfile
import unittest
import os
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from journal import Journal
from datetime import datetime, timedelta, timezone

from sheets import (EFLP_MARKER, GoogleSheets, GoogleSheetsError, HEADER, Reporter,
                    WEEKLY_MARKER, eflp_formulas, week_choices, weekly_formulas, weekly_start)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {'MEXC_P2_NICKNAME': '', 'MEXC_P2_2_NICKNAME': ''})
        self.env_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / 'cycles.sqlite3')
        self.cid = self.journal.create({'mode': 'api'})
        with patch('journal.now', return_value='2026-09-22T20:30:00+00:00'):
            self.journal.transition(self.cid, 'forward_complete', 'both', 'done', 'done',
                context={'amount': '9500', 'quantity': '107.5877'})
        self.journal.bind_sheet('sheet/tab')
        self.calls = []
        self.existing = [['Сумма продажи'], ['9500']]
        self.sheets = GoogleSheets.__new__(GoogleSheets)
        self.sheets.spreadsheet_id, self.sheets.tab, self.sheets.ready = 'sheet', 'tab', False
        self.sheets.get_sheet_id = AsyncMock(return_value=0)
        self.sheets.batch_update = AsyncMock(return_value={})
        self.existing_summary = []
        self.existing_eflp = []
        self.existing_eflp_meta = []
        async def request(method, cell_range, values=None):
            self.calls.append((method, cell_range, values))
            if method == 'GET':
                values_by_range = {'F:J': self.existing_summary, 'E:E': [],
                                   'N:P': self.existing_eflp, 'Q:S': self.existing_eflp_meta,
                                   'A:D': self.existing}
                return {'values': values_by_range[cell_range]}
            return {}
        self.sheets.request = request

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()
        self.env_patch.stop()

    async def test_migration_uses_usdt_and_sale_time_with_day_rollover(self):
        await self.sheets.prepare(self.journal)
        self.assertEqual(self.calls[-1], ('PUT', 'A1:D2', [HEADER, [107.5877, '23.09.2026', '03:30:00', 'default']]))
        self.assertEqual(self.journal.sales()[0]['amount'], '9500')

    async def test_foreign_cell_blocks_migration_without_write(self):
        for existing in ([['Сумма продажи'], ['123']], [['Сумма продажи'], ['9500', 'keep me']]):
            self.existing = existing
            with self.assertRaises(GoogleSheetsError):
                await self.sheets.prepare(self.journal)
        self.assertTrue(all(c[0] == 'GET' for c in self.calls))

    async def test_already_migrated_table_keeps_existing_rows(self):
        self.existing = [HEADER, [107.5877, '23.09.2026', '03:30:00', 'default']]
        await self.sheets.prepare(self.journal)
        self.assertEqual(self.calls[-1], ('PUT', 'A1:D1', [HEADER]))

    async def test_existing_profile_ids_become_saved_nicknames_without_changing_sales(self):
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?',
            (json.dumps({'mode': 'api', 'p2_profile': '2', 'nicknames': {'p2': 'kukish'}}), self.cid))
        self.journal.db.commit()
        self.existing = [HEADER, [107.5877, '23.09.2026', '03:30:00', '2']]
        await self.sheets.prepare(self.journal)
        self.assertIn(('PUT', 'D2:D2', [['kukish']]), self.calls)
        self.assertEqual(self.journal.sales()[0]['p2_nickname'], 'kukish')
        self.assertEqual(weekly_formulas(self.journal.sales(), '24.09.2026 18:50', 0)[11][0], 'kukish')

    def test_error_notification_names_saved_counterparty(self):
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?',
            (json.dumps({'mode': 'api', 'p2_profile': '2', 'nicknames': {'p2': 'kukish'}}), self.cid))
        self.journal.db.commit()
        event = dict(cycle_id=self.cid, step='reverse_create', actor='p2', status='error',
                     message='limit', order_no='', amount='100', fiat='RUB', quantity='1',
                     time='2026-09-22T20:30:00+00:00')
        reporter = Reporter(self.journal, type('Telegram', (), {'enabled': False})(), self.sheets)
        self.assertIn('П2: kukish', reporter.telegram_text(event))

    async def test_three_column_sheet_adds_saved_profile(self):
        self.existing = [HEADER[:3], ['107,5877', '23.09.2026', '03:30:00']]
        await self.sheets.prepare(self.journal)
        self.assertEqual(self.calls[-1], ('PUT', 'A1:D2',
            [HEADER, [107.5877, '23.09.2026', '03:30:00', 'default']]))

    def test_week_boundary_is_thursday_1850_moscow(self):
        before = datetime(2026, 10, 1, 15, 49, tzinfo=timezone.utc)
        after = datetime(2026, 10, 1, 15, 50, tzinfo=timezone.utc)
        self.assertEqual(weekly_start(after), after.astimezone(timezone(timedelta(hours=3))))
        self.assertEqual(weekly_start(before), weekly_start(after) - timedelta(days=7))
        sales = [dict(quantity='100', completed_at=before.isoformat(), p2_profile='old'),
                 dict(quantity='200', completed_at=after.isoformat(), p2_profile='2')]
        selected, choices = week_choices(sales, moment=after)
        self.assertEqual(selected, '01.10.2026 18:50')
        self.assertIn('24.09.2026 18:50', choices)
        rows = weekly_formulas(sales, selected, 0)
        self.assertEqual(rows[0][1], selected)
        self.assertEqual(rows[11][0], '2')
        self.assertIn('SUMIFS', rows[11][1])

    def test_weekly_cells_contain_formulas_and_keep_selected_week(self):
        moment = datetime(2026, 10, 1, 15, 50, tzinfo=timezone.utc)
        sale = dict(quantity='2500000', completed_at=moment.isoformat(), p2_profile='3')
        chosen, _ = week_choices([sale], '24.09.2026 18:50 МСК', moment)
        self.assertEqual(chosen, '24.09.2026 18:50')
        rows = weekly_formulas([sale], chosen, 0)
        self.assertIn('SUMIFS', rows[2][1])
        self.assertIn('IFS(', rows[2][3])
        self.assertEqual(rows[3][1], '=I3*5%')
        self.assertEqual(rows[8][3], '=200-SUM(I7:I8)')

    def test_eflp_formulas_use_selected_week_and_distinct_member_ids(self):
        sales = [dict(p1_profile='p1', p1_nickname='Maker',
                      scheduler_mode='eflp_volume', p2_member_id='buyer-1'),
                 dict(p1_profile='p1', p1_nickname='Maker',
                      scheduler_mode='eflp_unique', p2_member_id='buyer-2')]
        rows = eflp_formulas(sales, 0)
        self.assertEqual(rows[0][0], EFLP_MARKER)
        self.assertEqual(rows[1][0], 'Maker')
        self.assertIn('$E$1+7', rows[1][1])
        self.assertIn('COUNTUNIQUEIFS($S$2:$S', rows[1][2])

    async def test_eflp_summary_rejects_occupied_cells(self):
        self.existing_eflp = [['custom data']]
        with self.assertRaises(GoogleSheetsError):
            await self.sheets.send_weekly(self.journal.sales())
        self.sheets.batch_update.assert_not_awaited()

    async def test_weekly_summary_does_not_overwrite_occupied_neighbor_cells(self):
        async def request(method, area, values=None):
            self.calls.append((method, area, values))
            return {'values': [['', 'existing value']]} if method == 'GET' else {}
        self.sheets.request = request
        with self.assertRaises(GoogleSheetsError):
            await self.sheets.send_weekly(self.journal.sales())
        self.assertEqual(len(self.calls), 1)

    async def test_week_selection_survives_new_sale_refresh(self):
        self.existing_summary = [[WEEKLY_MARKER, '17.09.2026 18:50']]
        await self.sheets.send_weekly(self.journal.sales())
        requests = self.sheets.batch_update.await_args.args[0]
        selected = requests[1]['updateCells']['rows'][0]['values'][1]['userEnteredValue']['stringValue']
        self.assertEqual(selected, '17.09.2026 18:50')
        self.assertIn(selected, [v['userEnteredValue'] for v in
            requests[3]['setDataValidation']['rule']['condition']['values']])

    def test_legacy_journal_backfills_from_first_sale_event(self):
        self.journal.db.execute("UPDATE sales SET quantity='', completed_at=''")
        self.journal.db.commit()
        self.journal.close()
        self.journal = Journal(Path(self.temp.name) / 'cycles.sqlite3')
        sale = self.journal.sales()[0]
        self.assertEqual(sale['quantity'], '107.5877')
        self.assertEqual(sale['completed_at'], '2026-09-22T20:30:00+00:00')

    async def test_delivery_failure_alerts_once_and_retains_sale_for_retry(self):
        telegram = type('Telegram', (), {'enabled': True, 'send': AsyncMock(return_value=True)})()
        reporter = Reporter(self.journal, telegram, self.sheets)
        with patch.object(self.sheets, 'prepare', side_effect=GoogleSheetsError('Нет доступа')):
            await reporter.flush()
            await reporter.flush()
        self.assertEqual(telegram.send.await_count, 1)
        self.assertIn('sync-journal', telegram.send.call_args.args[0])
        self.assertEqual(len(self.journal.pending_sales()), 1)
        await reporter.flush()
        self.assertEqual(self.journal.pending_sales(), [])
        self.assertEqual(telegram.send.await_count, 1)

    async def test_sync_refreshes_weekly_without_pending_sale(self):
        self.journal.sale_delivered(self.journal.sales()[0]['id'])
        reporter = Reporter(self.journal, type('Telegram', (), {'enabled': False})(), self.sheets)
        await reporter.flush(force_sheets=True)
        requests = self.sheets.batch_update.await_args.args[0]
        chosen = requests[1]['updateCells']['rows'][0]['values'][1]['userEnteredValue']['stringValue']
        options = [value['userEnteredValue']
                   for value in requests[3]['setDataValidation']['rule']['condition']['values']]
        self.assertIn(chosen, options)
        self.assertEqual(requests[1]['updateCells']['rows'][2]['values'][1]['userEnteredValue']['formulaValue'][:7],
                         '=SUMIFS')
