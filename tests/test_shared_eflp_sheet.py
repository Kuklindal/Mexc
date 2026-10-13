import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from journal import Journal
from sheets import (EFLP_META_HEADER, HEADER, GoogleSheets, SHARED_CYCLE_HEADER,
                    eflp_meta_values, sale_values)
from trade_modes import next_profile, restore_eflp_progress


class SharedEflpSheetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.volume = Journal(Path(self.temp.name) / 'volume.sqlite3')
        self.unique = Journal(Path(self.temp.name) / 'unique.sqlite3')
        self.columns = {'A:E': [HEADER], 'R:T': [EFLP_META_HEADER],
                        'X:X': [], 'F:J': [], 'U1:U2': []}
        self.sheet = GoogleSheets.__new__(GoogleSheets)
        self.sheet.spreadsheet_id = 'sheet'
        self.sheet.tab = 'Eflp'
        self.sheet.shared_eflp = True
        self.sheet.shared_lock = Path(self.temp.name) / 'sheet.lock'
        self.sheet.ready = False
        self.sheet.get_sheet_id = AsyncMock(return_value=0)
        self.sheet.batch_update = AsyncMock(return_value={})

        async def batch_get(ranges):
            return [self.columns[cell_range] for cell_range in ranges]

        self.sheet.batch_get = AsyncMock(side_effect=batch_get)

        async def request(method, cell_range, values=None, *, value_render_option=None):
            if method == 'GET':
                return {'values': self.columns[cell_range]}
            column = next((key for key in ('A:E', 'R:T', 'X:X')
                           if cell_range.startswith(key.split(':')[0][0])), None)
            if cell_range.startswith('R'):
                column = 'R:T'
            elif cell_range.startswith('X'):
                column = 'X:X'
            else:
                column = 'A:E'
            first = cell_range.split(':', 1)[0]
            row_number = int(''.join(ch for ch in first if ch.isdigit()))
            rows = self.columns[column]
            while len(rows) < row_number - 1:
                rows.append([])
            for row in values:
                if len(rows) < row_number:
                    rows.append(row)
                else:
                    rows[row_number - 1] = row
                row_number += 1
            return {}

        self.sheet.request = request

    def tearDown(self):
        self.volume.close()
        self.unique.close()
        self.temp.cleanup()

    def sale(self, journal, p1, p2, member, mode='eflp_unique'):
        cid = journal.create({'mode': 'api', 'scheduler_mode': mode,
                              'p1_profile': p1, 'p2_profile': p2,
                              'members': {'p2': member}})
        journal.transition(cid, 'forward_complete', 'both', 'done', 'sold',
                           context={'amount': '1000', 'quantity': '75'})
        for step in ('reverse_complete', 'reverse_replenish'):
            journal.transition(cid, step, 'both', 'done', step,
                               cycle_status='completed' if step == 'reverse_replenish' else None)
        return journal.sales()[0]

    async def test_two_journals_append_to_one_tab_and_skip_completed_member(self):
        first = self.sale(self.volume, 'p1_3', '1', 'member-one', mode='eflp_volume')
        self.columns['A:E'].append(sale_values(first))
        self.columns['R:T'].append(eflp_meta_values(first, completed=True))
        self.volume.bind_sheet('sheet/Eflp')
        self.volume.sale_delivered(first['id'])

        await self.sheet.prepare(self.volume)
        self.sheet.batch_get.assert_awaited_with(['A:E', 'R:T', 'X:X'])
        self.assertEqual(self.columns['X:X'], [SHARED_CYCLE_HEADER, [first['cycle_id']]])
        self.sheet.ready = False
        second = self.sale(self.unique, 'p1_4', '2', 'member-two')
        await self.sheet.prepare(self.unique)
        await self.sheet.send(second)
        await self.sheet.send(second)
        self.assertEqual(len(self.columns['A:E']), 3)
        self.assertEqual(self.columns['X:X'][2], [second['cycle_id']])
        await self.sheet.send_weekly(self.unique.sales(), self.unique)
        self.assertEqual(self.columns['R:T'][1], eflp_meta_values(first, completed=True))
        self.assertEqual(self.columns['R:T'][2], eflp_meta_values(second, completed=True))

        # The second journal can count an already completed P2 from the same
        # tab, even though that sale belongs to the first journal.
        state = {'mode': 'eflp_unique', 'p1_profile': 'p1_3', 'profiles': ['1', '2'],
                 'cursor': 0, 'cooldowns': {}}
        sales = await self.sheet.read_eflp_sales(self.unique)
        restore_eflp_progress(self.unique, state, sales, env={
            'MEXC_P2_1_MEMBER_ID': 'member-one',
            'MEXC_P2_2_MEMBER_ID': 'member-two'})
        self.assertEqual(state['eflp_unique_member_ids'], ['member-one'])
        self.assertEqual(next_profile(state)[0], '2')

    async def test_claimed_row_is_reused_after_interrupted_write(self):
        sale = self.sale(self.unique, 'p1_4', '2', 'member-two')
        await self.sheet.prepare(self.unique)
        self.columns['X:X'].append([sale['cycle_id']])
        await self.sheet.send(sale)
        await self.sheet.send(sale)
        self.assertEqual(len(self.columns['A:E']), 2)
        self.assertEqual(self.columns['A:E'][1], sale_values(sale))
        self.assertEqual(self.columns['R:T'][1], eflp_meta_values(sale))


if __name__ == '__main__':
    unittest.main()
