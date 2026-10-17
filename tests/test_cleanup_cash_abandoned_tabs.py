import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock

from browser_housekeeping import (abandoned_orders, cleanup_abandoned_cash_tabs,
                                  cleanup_terminal_order_tabs, is_final_order,
                                  terminal_orders)


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


class CleanupBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_only_terminal_abandoned_tab(self):
        old = 'd1111111111111111111'
        unsettled = 'd3333333333333333333'
        paused = 'd2222222222222222222'
        with sqlite3.connect(':memory:') as db:
            db.executescript('''
                CREATE TABLE cycles(id TEXT PRIMARY KEY, status TEXT);
                CREATE TABLE steps(cycle_id TEXT, name TEXT, status TEXT, result TEXT);
            ''')
            for cycle_id, status, number in (('old', 'abandoned', old),
                                             ('paused', 'paused', paused),
                                             ('unsettled', 'abandoned', unsettled)):
                db.execute('INSERT INTO cycles VALUES (?, ?)', (cycle_id, status))
                db.execute('INSERT INTO steps VALUES (?, ?, ?, ?)',
                           (cycle_id, 'forward_create', 'done',
                            json.dumps({'order_no': number})))
            call = AsyncMock(return_value={'targetInfos': [
                {'type': 'page', 'url': 'https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=' + old},
                {'type': 'page', 'url': 'https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=' + paused},
                {'type': 'page', 'url': 'https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=' + unsettled},
            ]})
            browser = MagicMock()
            browser.connection.return_value.__aenter__ = AsyncMock(return_value=call)
            browser.connection.return_value.__aexit__ = AsyncMock(return_value=None)
            browser.close_order_tabs = AsyncMock(return_value=1)
            client = MagicMock()
            client.get_order_detail = AsyncMock(side_effect=[
                {'advOrderNo': old, 'state': 'DONE'},
                {'advOrderNo': unsettled, 'state': 'PAID'},
            ])

            self.assertEqual(await cleanup_abandoned_cash_tabs(db, browser, client), (2, 1, 1, 1))
            self.assertEqual(client.get_order_detail.await_count, 2)
            browser.close_order_tabs.assert_awaited_once_with([old])

    async def test_completed_cycle_tab_is_reclaimed_but_paused_tab_is_preserved(self):
        finished = 'd4444444444444444444'
        paused = 'd5555555555555555555'
        with sqlite3.connect(':memory:') as db:
            db.executescript('''
                CREATE TABLE cycles(id TEXT PRIMARY KEY, status TEXT);
                CREATE TABLE steps(cycle_id TEXT, name TEXT, status TEXT, result TEXT);
            ''')
            for cycle_id, status, number in (('finished', 'completed', finished),
                                             ('paused', 'paused', paused)):
                db.execute('INSERT INTO cycles VALUES (?, ?)', (cycle_id, status))
                db.execute('INSERT INTO steps VALUES (?, ?, ?, ?)',
                           (cycle_id, 'reverse_create', 'done',
                            json.dumps({'order_no': number})))
            self.assertEqual(terminal_orders(db, frozenset({'completed', 'abandoned'})),
                             {finished})
            call = AsyncMock(return_value={'targetInfos': [
                {'type': 'page', 'url': 'https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=' + finished},
                {'type': 'page', 'url': 'https://www.mexc.com/ru-RU/buy-crypto/order-processing?id=' + paused},
            ]})
            browser = MagicMock()
            browser.connection.return_value.__aenter__ = AsyncMock(return_value=call)
            browser.connection.return_value.__aexit__ = AsyncMock(return_value=None)
            browser.close_order_tabs = AsyncMock(return_value=1)
            client = MagicMock()
            client.get_order_detail = AsyncMock(return_value={
                'advOrderNo': finished, 'state': 'DONE'})

            self.assertEqual(await cleanup_terminal_order_tabs(db, browser, client),
                             (1, 1, 1, 0))
            client.get_order_detail.assert_awaited_once_with(finished)
            browser.close_order_tabs.assert_awaited_once_with([finished])


if __name__ == '__main__':
    unittest.main()
