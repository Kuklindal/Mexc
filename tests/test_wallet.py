import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from journal import Journal
from wallet import WalletTransfers


class WalletTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / 'cycles.sqlite3')
        self.console = type('Console', (), {'confirm': lambda *args: None, 'write': lambda *args: None})()
        self.wallet = WalletTransfers(self.journal, self.console)
        self.client = type('Client', (), {})()
        self.client.transfer_usdt = AsyncMock(return_value='TX-1')
        self.client.get_wallet_transfer = AsyncMock(return_value={
            'tranId': 'TX-1', 'asset': 'USDT', 'amount': '2.5000',
            'fromAccountType': 'OTC', 'toAccountType': 'SPOT', 'status': 'SUCCESS',
            'timestamp': 0})
        self.spec = {'account': 'p2', 'p2_profile': 'default', 'key_hash': 'key',
                     'source': 'OTC', 'target': 'SPOT', 'amount': '2.5'}

    async def asyncTearDown(self):
        self.journal.close()
        self.temp.cleanup()

    async def test_success_is_recorded_and_verified(self):
        self.assertEqual(await self.wallet.send(self.client, self.spec), 0)
        row = self.journal.db.execute('SELECT * FROM wallet_transfers').fetchone()
        self.assertEqual((row['status'], row['tran_id']), ('SUCCESS', 'TX-1'))
        self.client.transfer_usdt.assert_awaited_once_with('OTC', 'SPOT', '2.5')

    async def test_lost_response_blocks_duplicate_transfer(self):
        self.client.transfer_usdt.side_effect = TimeoutError('Response lost')
        with self.assertRaises(TimeoutError):
            await self.wallet.send(self.client, self.spec)
        row = self.journal.db.execute('SELECT * FROM wallet_transfers').fetchone()
        self.assertEqual(row['status'], 'in_flight')
        with self.assertRaisesRegex(RuntimeError, 'несверенный перевод'):
            await self.wallet.send(self.client, self.spec)
        self.assertEqual(self.client.transfer_usdt.await_count, 1)
        with self.assertRaisesRegex(RuntimeError, 'tranId'):
            await self.wallet.check(self.client, row['id'])

    async def test_mismatched_history_is_not_accepted(self):
        self.client.get_wallet_transfer.return_value['toAccountType'] = 'FUTURES'
        with self.assertRaisesRegex(RuntimeError, 'не совпали'):
            await self.wallet.send(self.client, self.spec)
        row = self.journal.db.execute('SELECT * FROM wallet_transfers').fetchone()
        self.assertEqual(row['status'], 'submitted')
