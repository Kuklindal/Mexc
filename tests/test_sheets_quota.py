import unittest
from unittest.mock import AsyncMock, patch

import httpx

from sheets import GoogleSheets, GoogleSheetsRateLimited


def response(status, data=None):
    return httpx.Response(status, json=data or {},
                          request=httpx.Request('GET', 'https://sheets.googleapis.com/'))


class SheetsQuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sheet = GoogleSheets.__new__(GoogleSheets)
        self.sheet.spreadsheet_id = 'spreadsheet'
        self.sheet.tab = 'Eflp'
        self.sheet.token = AsyncMock(return_value='test-token')

    async def test_shared_ranges_use_one_batch_read_and_retry_429(self):
        result = {'valueRanges': [
            {'values': [['sale']]}, {'values': [['meta']]}, {'values': [['cycle']]}
        ]}
        with patch('sheets.httpx.AsyncClient') as client_type, \
             patch('sheets.asyncio.sleep', new_callable=AsyncMock) as sleep, \
             patch('sheets.random.random', return_value=0):
            client = client_type.return_value.__aenter__.return_value
            client.request = AsyncMock(side_effect=[response(429), response(200, result)])
            rows = await self.sheet.batch_get(['A:E', 'R:T', 'X:X'])

        self.assertEqual(rows, [[['sale']], [['meta']], [['cycle']]])
        self.assertEqual(client.request.await_count, 2)
        self.assertEqual(client.request.await_args.kwargs['params'], [
            ('ranges', "'Eflp'!A:E"), ('ranges', "'Eflp'!R:T"),
            ('ranges', "'Eflp'!X:X")])
        sleep.assert_awaited_once_with(1)

    async def test_persistent_429_remains_distinguishable_from_bad_config(self):
        with patch('sheets.httpx.AsyncClient') as client_type, \
             patch('sheets.asyncio.sleep', new_callable=AsyncMock) as sleep, \
             patch('sheets.random.random', return_value=0):
            client = client_type.return_value.__aenter__.return_value
            client.request = AsyncMock(return_value=response(429))
            with self.assertRaises(GoogleSheetsRateLimited):
                await self.sheet.request('GET', 'A:E')

        self.assertEqual(client.request.await_count, 8)
        self.assertEqual(sleep.await_count, 7)
