from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from journal import Journal
from volume_policy import (cooldown_until, record_purchase, remaining_usdt,
                           rolling_cash_purchases, rolling_cash_retry_at,
                           should_rotate, window_for)


class VolumePolicyTests(unittest.TestCase):
    def test_third_trade_starts_24_hour_timer_and_purchase_is_idempotent(self):
        state = {}
        at = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)
        for index in range(3):
            window = record_purchase(state, 'account', f'cycle-{index}', '2000',
                                     at + timedelta(minutes=index))
        record_purchase(state, 'account', 'cycle-2', '2000', at + timedelta(minutes=2))
        self.assertEqual(Decimal(window['quantity']), Decimal('6000'))
        self.assertEqual(len(window['orders']), 3)
        self.assertEqual(cooldown_until(window), at + timedelta(days=1, minutes=2))
        self.assertIs(window_for(state, 'account', at + timedelta(hours=23)), window)
        self.assertEqual(window_for(state, 'account', cooldown_until(window))['quantity'], '0')

    def test_rotate_at_70000_or_before_next_order_crosses_71000(self):
        window = {'quantity': '70000'}
        self.assertTrue(should_rotate(window))
        window['quantity'] = '69500'
        self.assertFalse(should_rotate(window, Decimal('1500')))
        self.assertTrue(should_rotate(window, Decimal('1500.0001')))
        self.assertEqual(remaining_usdt(window), Decimal('1500'))

    def test_rolling_cash_uses_confirmed_first_legs_across_modes_and_abandoned_cycles(self):
        with TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'cycles.sqlite3')
            now = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)
            def purchase(profile, quantity, created, member):
                cycle_id = journal.create({'p2_profile': profile, 'members': {'p2': member}})
                with patch('journal.now', return_value=created.isoformat()):
                    journal.transition(cycle_id, 'forward_create', 'p2', 'done', 'created')
                    journal.transition(cycle_id, 'forward_complete', 'both', 'done', 'sale',
                                       result={'quantity': quantity},
                                       context={'amount': '1000', 'quantity': quantity})
                journal.abandon(cycle_id)
                return cycle_id
            recent = purchase('one', '1000', now - timedelta(hours=23), 'member-one')
            purchase('one', '5000', now - timedelta(days=1, seconds=1), 'member-one')
            purchase('other', '2000', now - timedelta(hours=2), 'member-other')
            renamed = purchase('old-name', '3000', now - timedelta(hours=1), 'member-one')
            purchase('one', '9000', now - timedelta(minutes=45), 'reused-profile-different-member')
            window = rolling_cash_purchases(journal, 'one', now, 'member-one')
            self.assertEqual(Decimal(window['quantity']), Decimal('4000'))
            self.assertEqual({item['cycle_id'] for item in window['orders']}, {recent, renamed})
            self.assertEqual(rolling_cash_retry_at(window), now + timedelta(hours=1))
            self.assertEqual(rolling_cash_purchases(journal, 'one', now + timedelta(hours=2),
                                                    'member-one')['quantity'], '3000')
            uncertain = journal.create({'p2_profile': 'one', 'members': {'p2': 'member-one'}})
            with patch('journal.now', return_value=(now - timedelta(minutes=30)).isoformat()):
                journal.transition(uncertain, 'forward_create', 'p2', 'done', 'created',
                                   result={'order_no': 'unknown-order'})
            journal.abandon(uncertain)
            blocked = rolling_cash_purchases(journal, 'one', now, 'member-one')
            self.assertEqual(blocked['quantity'], '4000')
            self.assertEqual(rolling_cash_retry_at(blocked), now + timedelta(hours=23, minutes=30))
            journal.close()
