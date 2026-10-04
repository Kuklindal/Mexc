import json
import unittest
from unittest.mock import AsyncMock, patch

from cycle import Paused, run_series
from mexc_client import MexcAPIError
import test_auto as fixtures


class DailyLimitTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown
    runner = fixtures.AutoTests.runner

    def reject(self, runner, *, reverse=True, http_status=200, code=60085):
        original = runner.clients['p2'].create_order

        async def create(**params):
            if ('tradable_quantity' in params) == reverse:
                raise MexcAPIError('Order rejected', code=code, http_status=http_status)
            return await original(**params)

        runner.clients['p2'].create_order = AsyncMock(side_effect=create)
        return runner.clients['p2'].create_order

    async def test_rejection_stops_series_without_new_order_or_refill(self):
        runner = self.runner()
        spec = self.journal.cycle(self.cycle_id)['spec']
        spec['series'] = dict(id='SERIES', index=1, count=3, min_amount='10000', max_amount='10000', fiat='RUB')
        with self.journal.db:
            self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(spec), self.cycle_id))
        create = self.reject(runner)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaisesRegex(Paused, '60085'):
            await run_series(runner, self.cycle_id)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertEqual(self.exchange.orders['ORDER-1']['state'], 'DONE')
        self.assertEqual(self.exchange.ad_calls, [])
        self.assertEqual(len(self.journal.cycles()), 1)
        self.assertEqual(self.journal.cycle(self.cycle_id)['status'], 'paused')
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_create')['status'], 'rejected')
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn('60085', self.telegram.messages[0])
        self.assertIn('100 USDT', self.telegram.messages[0])
        self.assertEqual(len(self.journal.sales()), 1)
        with self.assertRaisesRegex(Paused, '60085'):
            await run_series(runner, self.cycle_id)
        self.assertEqual(create.await_count, 2)
        runner.browser.close_order_tabs.assert_not_called()

    async def test_first_order_limit_is_also_definite_rejection(self):
        runner = self.runner()
        create = self.reject(runner, reverse=False, http_status=400)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaisesRegex(Paused, '60085'):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_create')['status'], 'rejected')
        with self.assertRaisesRegex(Paused, '60085'):
            await runner.run(self.cycle_id)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(self.exchange.orders, {})
        self.assertEqual(self.journal.sales(), [])

    async def test_ad_rejection_before_purchase_creates_no_order(self):
        runner = self.runner()
        create = self.reject(runner, reverse=False, http_status=400, code=85010)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaisesRegex(Paused, '85010'):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_create')['result']['rejected_code'], 85010)
        with self.assertRaisesRegex(Paused, '85010'):
            await runner.run(self.cycle_id)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(self.exchange.orders, {})

    async def test_ad_rejection_after_purchase_preserves_usdt_for_return(self):
        runner = self.runner()
        create = self.reject(runner, code=85010)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaisesRegex(Paused, '85010'):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_complete')['status'], 'done')
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_create')['result']['rejected_code'], 85010)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(self.exchange.orders['ORDER-1']['state'], 'DONE')

    async def test_legacy_forward_ad_rejection_is_recovered_without_order_retry(self):
        runner = self.runner()
        create = self.reject(runner, reverse=False, code=85010)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.journal.transition(self.cycle_id, 'forward_create', 'p2', 'unknown', 'Legacy uncertain result')
        original_time = '2026-10-01T00:37:05+00:00'
        with patch('journal.now', return_value=original_time):
            self.journal.transition(self.cycle_id, 'forward_create', 'p2', 'error',
                'POST /api/v3/fiat/merchant/order/deal: MEXC error: {"code":85010}')
        with self.assertRaisesRegex(Paused, '85010'):
            await runner.run(self.cycle_id)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(self.journal.step(self.cycle_id, 'forward_create')['result']['rejected_code'], 85010)
        self.assertEqual(self.journal.create_rejection_details(self.cycle_id, 'forward_create'),
                         (85010, original_time))

    async def test_server_error_is_not_treated_as_definite_rejection(self):
        runner = self.runner()
        create = self.reject(runner, http_status=500)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(MexcAPIError):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_create')['status'], 'unknown')
        with self.assertRaisesRegex(Paused, 'сверки'):
            await runner.run(self.cycle_id)
        self.assertEqual(create.await_count, 2)

    async def test_legacy_rejection_is_restored_without_repeating_request(self):
        runner = self.runner()
        create = self.reject(runner)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        self.journal.transition(self.cycle_id, 'reverse_create', 'p2', 'unknown', 'Legacy uncertain result')
        message = 'POST /api/v3/fiat/merchant/order/deal: MEXC error: {"code":60085,"msg":"Daily limit"}'
        self.journal.transition(self.cycle_id, 'reverse_create', 'p2', 'error', message)
        with self.assertRaisesRegex(Paused, '60085'):
            await runner.run(self.cycle_id)
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_create')['status'], 'rejected')
        self.assertEqual(create.await_count, 2)

    def test_legacy_detection_ignores_old_and_unrelated_errors(self):
        message = 'POST /api/v3/fiat/merchant/order/deal: MEXC error: {"code":60085}'
        for value in ('TimeoutError', 'GET /else: MEXC error: {"code":60085}', message + 'invalid'):
            self.journal.transition(self.cycle_id, 'reverse_create', 'p2', 'error', value)
            self.assertFalse(self.journal.reverse_daily_limit_rejected(self.cycle_id))
        self.journal.transition(self.cycle_id, 'reverse_create', 'p2', 'error', message)
        self.assertTrue(self.journal.reverse_daily_limit_rejected(self.cycle_id))
        self.journal.transition(self.cycle_id, 'reverse_create', 'p2', 'in_flight', 'New attempt')
        self.assertFalse(self.journal.reverse_daily_limit_rejected(self.cycle_id))

    async def test_interactive_stop_preserves_rejection_without_request(self):
        runner = self.runner()
        create = self.reject(runner)
        with patch('cycle.asyncio.sleep', new=AsyncMock()), self.assertRaises(Paused):
            await runner.run(self.cycle_id)
        runner.automatic = False
        runner.console = self.operator
        self.operator.pause_on = 'ЛИМИТ ПРОВЕРЕН'
        with self.assertRaisesRegex(Paused, 'Test pause'):
            await runner.run(self.cycle_id)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(self.journal.step(self.cycle_id, 'reverse_create')['status'], 'rejected')
