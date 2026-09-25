import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from journal import Journal
from sheets import GoogleSheets, GoogleSheetsError, HEADER, Reporter


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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
        async def request(method, cell_range, values=None):
            self.calls.append((method, cell_range, values))
            return {'values': self.existing} if method == 'GET' else {}
        self.sheets.request = request

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    async def test_migration_uses_usdt_and_sale_time_with_day_rollover(self):
        await self.sheets.prepare(self.journal)
        self.assertEqual(self.calls[-1], ('PUT', 'A1:C2', [HEADER, [107.5877, '23.09.2026', '03:30:00']]))
        self.assertEqual(self.journal.sales()[0]['amount'], '9500')

    async def test_foreign_cell_blocks_migration_without_write(self):
        for existing in ([['Сумма продажи'], ['123']], [['Сумма продажи'], ['9500', 'keep me']]):
            self.existing = existing
            with self.assertRaises(GoogleSheetsError):
                await self.sheets.prepare(self.journal)
        self.assertTrue(all(c[0] == 'GET' for c in self.calls))

    async def test_already_migrated_table_keeps_existing_rows(self):
        self.existing = [HEADER, [107.5877, '23.09.2026', '03:30:00']]
        await self.sheets.prepare(self.journal)
        self.assertEqual(self.calls[-1], ('PUT', 'A1:C1', [HEADER]))

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
