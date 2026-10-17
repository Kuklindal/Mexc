import json
import sqlite3
import unittest

from deploy.cleanup_cash_abandoned_tabs import abandoned_orders, is_final_order


class AbandonedTabCleanupTests(unittest.TestCase):
    def test_only_orders_exclusive_to_abandoned_cycles_are_candidates(self):
        with sqlite3.connect(':memory:') as db:
            db.executescript('''
                CREATE TABLE cycles(id TEXT PRIMARY KEY, status TEXT);
                CREATE TABLE steps(cycle_id TEXT, name TEXT, status TEXT, result TEXT);
            ''')
            for cycle_id, status, number in (
                    ('old', 'abandoned', 'd1111111111111111111'),
                    ('paused', 'paused', 'd2222222222222222222'),
                    ('shared-old', 'abandoned', 'd3333333333333333333'),
                    ('shared-live', 'active', 'd3333333333333333333')):
                db.execute('INSERT INTO cycles VALUES (?, ?)', (cycle_id, status))
                db.execute('INSERT INTO steps VALUES (?, ?, ?, ?)',
                           (cycle_id, 'forward_create', 'done',
                            json.dumps({'order_no': number})))
            self.assertEqual(abandoned_orders(db), {'d1111111111111111111'})

    def test_only_final_state_of_exact_order_is_accepted(self):
        number = 'd1111111111111111111'
        self.assertTrue(is_final_order({'advOrderNo': number, 'state': 'DONE'}, number))
        self.assertTrue(is_final_order({'advOrderNo': number, 'state': 'CANCEL'}, number))
        self.assertFalse(is_final_order({'advOrderNo': number, 'state': 'PAID'}, number))
        self.assertFalse(is_final_order({'advOrderNo': 'd2222222222222222222',
                                         'state': 'DONE'}, number))


if __name__ == '__main__':
    unittest.main()
