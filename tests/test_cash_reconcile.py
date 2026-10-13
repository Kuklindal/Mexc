import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deploy import reconcile_canceled_cash_order as reconcile
from journal import Journal
from rollover import load_state, save_state
from volume_policy import rolling_cash_purchases


class CashReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_cancellation_removes_only_its_uncertain_timer(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'cycles.sqlite3'
            journal = Journal(path)
            cycle_id = journal.create({'p2_profile': '28'})
            journal.transition(cycle_id, 'forward_create', 'p2', 'done', 'created',
                               result={'order_no': 'test-order'})
            journal.abandon(cycle_id)
            save_state(journal, {'mode': 'cash_volume', 'status': 'paused',
                                 'profiles': ['28', '29'], 'active_cycle': None,
                                 'cooldowns': {'28': {'reason': 'volume_rolling',
                                                      'until': '2099-01-01T00:00:00+00:00'},
                                               '29': {'reason': '85010',
                                                      'until': '2099-01-01T00:00:00+00:00'}}})
            journal.close()
            detail = {'state': 'CANCEL', 'tradableQuantity': '2128.6472'}
            with patch.dict(os.environ, {'CYCLE_DB': str(path)}), \
                 patch.object(sys, 'argv', ['reconcile', 'test-order', '28']), \
                 patch.object(reconcile, 'load_dotenv'), \
                 patch.object(reconcile, 'stopped_service', return_value=True), \
                 patch.object(reconcile, 'order_detail', side_effect=[detail, detail]):
                await reconcile.main()
            journal = Journal(path)
            self.assertEqual(journal.step(cycle_id, 'forward_cancel_confirmed')['status'], 'done')
            self.assertIsNone(rolling_cash_purchases(journal, '28')['uncertain_until'])
            self.assertNotIn('28', load_state(journal)['cooldowns'])
            self.assertIn('29', load_state(journal)['cooldowns'])
            journal.close()

    async def test_one_account_not_cancelled_preserves_journal(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'cycles.sqlite3'
            journal = Journal(path)
            cycle_id = journal.create({'p2_profile': '28'})
            journal.transition(cycle_id, 'forward_create', 'p2', 'done', 'created',
                               result={'order_no': 'test-order'})
            journal.abandon(cycle_id)
            save_state(journal, {'mode': 'cash_volume', 'status': 'paused',
                                 'profiles': ['28'], 'active_cycle': None,
                                 'cooldowns': {'28': {'reason': 'volume_rolling',
                                                      'until': '2099-01-01T00:00:00+00:00'}}})
            journal.close()
            p1 = {'state': 'CANCEL', 'tradableQuantity': '2128.6472'}
            p2 = {'state': 'DONE', 'tradableQuantity': '2128.6472'}
            with patch.dict(os.environ, {'CYCLE_DB': str(path)}), \
                 patch.object(sys, 'argv', ['reconcile', 'test-order', '28']), \
                 patch.object(reconcile, 'load_dotenv'), \
                 patch.object(reconcile, 'stopped_service', return_value=True), \
                 patch.object(reconcile, 'order_detail', side_effect=[p1, p2]):
                with self.assertRaisesRegex(SystemExit, 'не подтверждена'):
                    await reconcile.main()
            journal = Journal(path)
            self.assertIsNone(journal.step(cycle_id, 'forward_cancel_confirmed'))
            self.assertIn('28', load_state(journal)['cooldowns'])
            journal.close()
