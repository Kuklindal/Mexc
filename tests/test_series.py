import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from cycle import Paused, auto_plan, random_amount, run_series
from main import build_parser
import test_auto as fixtures


class SeriesTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown
    runner = fixtures.AutoTests.runner
    outsider = fixtures.AutoTests.outsider

    def plan(self, *options, env=None):
        args = build_parser().parse_args(['cycle', '--auto', *options])
        return auto_plan(args, env or {})

    def attach_series(self, count=3):
        runner = self.runner()
        spec = self.journal.cycle(self.cycle_id)['spec']
        spec.update(amount='9000', series=self.plan('--min-amount', '9000', '--max-amount', '9500', '--count', str(count)))
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(spec), self.cycle_id))
        self.journal.db.commit()
        return runner

    def test_range_is_inclusive_and_decimal_exact(self):
        plan = self.plan('--min-amount', '9000,01', '--max-amount', '9500.99')
        with patch('cycle.random.randint', side_effect=[900001, 950099]) as draw:
            self.assertEqual(random_amount(plan), '9000.01')
            self.assertEqual(random_amount(plan), '9500.99')
        draw.assert_called_with(900001, 950099)

    def test_bad_bounds_counts_and_conflicting_options_rejected(self):
        for options in (
            ['--min-amount', '9500', '--max-amount', '9000'],
            ['--min-amount', '0', '--max-amount', '9000'],
            ['--min-amount', 'NaN', '--max-amount', '9000'],
            ['--min-amount', '9000.001', '--max-amount', '9500'],
            ['--min-amount', '9000'],
            ['--amount', '9000 RUB', '--count', '0'],
            ['--amount', '9000 RUB', '--count', '-1'],
            ['--amount', '9000 RUB', '--max-amount', '9500'],
            ['--amount', '9000 RUB', '--fiat', 'KZT'],
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.plan(*options)

    def test_env_defaults_cli_override_and_fixed_amount(self):
        env = dict(AUTO_MIN_AMOUNT='9000', AUTO_MAX_AMOUNT='9500', AUTO_FIAT='RUB')
        plan = self.plan(env=env)
        self.assertEqual((plan['min_amount'], plan['max_amount'], plan['count']), ('9000', '9500', 1))
        plan = self.plan('--min-amount', '9100', '--max-amount', '9200', '--count', '2', env=env)
        self.assertEqual((plan['min_amount'], plan['max_amount'], plan['count']), ('9100', '9200', 2))
        plan = self.plan('--amount', '9150 RUB', env=env)
        self.assertEqual(random_amount(plan), '9150')
        self.assertEqual(plan['count'], 1)

    async def test_exact_cycle_count_refill_and_no_repeat_on_resume(self):
        runner = self.attach_series()
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await run_series(runner, self.cycle_id)
        self.assertEqual(len(self.exchange.orders), 6)
        self.assertEqual(len(self.exchange.ad_calls), 3)
        self.assertEqual(len(self.journal.sales()), 3)
        self.assertEqual(self.telegram.messages, [])
        for row in self.journal.cycles():
            saved = self.journal.cycle(row['id'])
            self.assertEqual(saved['status'], 'completed')
            self.assertTrue(Decimal('9000') <= Decimal(saved['spec']['amount']) <= Decimal('9500'))
        calls = len(self.exchange.calls)
        await run_series(runner, self.cycle_id)
        self.assertEqual(len(self.exchange.calls), calls)
        self.assertEqual(len(self.journal.cycles()), 3)

    async def test_named_p2_remains_bound_throughout_series(self):
        runner = self.attach_series()
        runner.p2_profile = '2'
        spec = self.journal.cycle(self.cycle_id)['spec']
        spec.update(p2_profile='2', p2_payment_id=runner.p2_payment_id,
                    members=runner.trusted_members, nicknames=runner.trusted_nicknames)
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(spec), self.cycle_id))
        self.journal.db.commit()
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await run_series(runner, self.cycle_id)
        for row in self.journal.cycles():
            saved = self.journal.cycle(row['id'])['spec']
            self.assertEqual(saved['p2_profile'], '2')
            self.assertEqual(saved['p2_payment_id'], runner.p2_payment_id)
            self.assertEqual(saved['members'], runner.trusted_members)
            self.assertEqual(saved['nicknames'], runner.trusted_nicknames)

    async def test_resume_from_earlier_id_preserves_pending_amount_and_remaining_count(self):
        runner = self.attach_series()
        original_run = runner.run
        async def pause_second(cid):
            if self.journal.cycle(cid)['spec']['series']['index'] == 2:
                raise Paused('STOP')
            await original_run(cid)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), patch.object(runner, 'run', side_effect=pause_second):
            with self.assertRaises(Paused):
                await run_series(runner, self.cycle_id)
        plan = self.journal.cycle(self.cycle_id)['spec']['series']
        second_id = self.journal.series_cycle(plan['id'], 2)
        saved_amount = self.journal.cycle(second_id)['spec']['amount']
        self.assertEqual(len(self.exchange.orders), 2)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), patch('cycle.random.randint', return_value=950000) as draw:
            await run_series(runner, self.cycle_id)
        self.assertEqual(draw.call_count, 1)  # Only the third cycle needs a new amount.
        self.assertEqual(self.journal.cycle(second_id)['spec']['amount'], saved_amount)
        self.assertEqual(len(self.exchange.orders), 6)

    async def test_outsider_does_not_stop_remaining_series(self):
        runner = self.attach_series()
        original_run = runner.run
        async def outsider_after_first(cid):
            await original_run(cid)
            self.outsider()
        with patch('cycle.asyncio.sleep', new=AsyncMock()), patch.object(runner, 'run', side_effect=outsider_after_first):
            await run_series(runner, self.cycle_id)
        self.assertEqual(sum(op == 'create' for _, op, _ in self.exchange.calls), 6)
        self.assertEqual(len(self.journal.sales()), 3)
        self.assertEqual(self.exchange.orders['OUTSIDER']['state'], 'NOT_PAID')
        self.assertFalse(any(data == 'OUTSIDER' for _, _, data in self.exchange.calls))

    async def test_ambiguous_request_stops_series_without_next_cycle(self):
        runner = self.attach_series()
        self.exchange.fail = 'create'
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(TimeoutError):
            await run_series(runner, self.cycle_id)
        self.assertEqual(len(self.journal.cycles()), 1)
        self.assertEqual(sum(op == 'create' for _, op, _ in self.exchange.calls), 1)

    async def test_interactive_recovery_does_not_launch_remaining_cycles(self):
        runner = self.attach_series()
        # Execute only the first cycle as a stand-in for completed interactive recovery.
        with patch('cycle.asyncio.sleep', new=AsyncMock()):
            await runner.run(self.cycle_id)
        runner.automatic = False
        await run_series(runner, self.cycle_id)
        self.assertEqual(len(self.journal.cycles()), 1)
