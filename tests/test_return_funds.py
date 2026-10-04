import asyncio
import os
from unittest.mock import AsyncMock, patch
import unittest

from cycle import Paused
from return_funds import ReturnFunds
from rollover import save_state
import test_auto as fixtures


class ReturnFundsTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AutoTests.setUp
    tearDown = fixtures.AutoTests.tearDown

    def setup_return(self):
        self.spec.update(forward_adv_no='AD-SELL', fiat='RUB')
        self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?',
            (__import__('json').dumps(self.spec), self.cycle_id))
        self.journal.db.commit()
        self.journal.transition(self.cycle_id, 'forward_complete', 'both', 'done', 'done',
                                result={'quantity': '100'}, context={'amount': '9000', 'fiat': 'RUB', 'quantity': '100'})
        state = {'pending_return': {'cycle_id': self.cycle_id, 'profile': 'default'}}
        save_state(self.journal, state)
        network = {'netWork': 'PLASMA', 'depositEnable': True, 'withdrawEnable': True,
                   'contract': 'CONTRACT', 'withdrawFee': '0', 'withdrawMin': '10',
                   'withdrawMax': '100000', 'withdrawIntegerMultiple': '6'}
        coins = [{'coin': 'USDT', 'networkList': [network]}]
        address = '0x' + '1' * 40
        output = {'coin': 'USDT', 'network': 'PLASMA', 'address': address, 'memo': None}
        withdrawal = {'id': 'WITHDRAW', 'withdrawOrderId': '', 'coin': 'USDT', 'network': 'PLASMA',
                      'address': address, 'amount': '100.000000', 'memo': '', 'status': 7,
                      'txId': 'TX-CHAIN', 'transferType': 0, 'transactionFee': '0'}
        deposit = {'coin': 'USDT', 'network': 'PLASMA', 'address': address, 'memo': None,
                   'status': 5, 'txId': 'TX-CHAIN', 'amount': '100'}
        p1 = type('Client', (), {'api_key': 'P1'})()
        p2 = type('Client', (), {'api_key': 'P2'})()
        async def p1_list(path, params=None):
            if path.endswith('getall'): return coins
            if path.endswith('address'): return [output]
            if path.endswith('hisrec'): return [deposit]
            raise AssertionError(path)
        async def p2_list(path, params=None):
            if path.endswith('getall'): return coins
            if path.endswith('withdraw/history'):
                withdrawal['withdrawOrderId'] = state['pending_return']['request_id']
                return [withdrawal]
            raise AssertionError(path)
        p1.wallet_list = AsyncMock(side_effect=p1_list)
        p2.wallet_list = AsyncMock(side_effect=p2_list)
        p1.transfer_usdt = AsyncMock(return_value='P1-TRANSFER')
        p2.transfer_usdt = AsyncMock(return_value='P2-TRANSFER')
        p2.withdraw_usdt = AsyncMock(return_value='WITHDRAW')
        p1.get_wallet_transfer = AsyncMock(return_value={
            'tranId': 'P1-TRANSFER', 'asset': 'USDT', 'amount': '100',
            'fromAccountType': 'SPOT', 'toAccountType': 'OTC', 'status': 'SUCCESS'})
        p2.get_wallet_transfer = AsyncMock(return_value={
            'tranId': 'P2-TRANSFER', 'asset': 'USDT', 'amount': '100',
            'fromAccountType': 'OTC', 'toAccountType': 'SPOT', 'status': 'SUCCESS'})
        p1.get_ad = AsyncMock(return_value={'advNo': 'AD-SELL', 'availableQuantity': '5'})
        browser = type('Browser', (), {})()
        actual = {'id': 'AD-SELL', 'coinName': 'USDT', 'tradeType': 1, 'currency': 'RUB',
                  'availableQuantity': '5', 'overVerify': {'types': [1]}}
        browser.ad_details = AsyncMock(side_effect=lambda _: actual.copy())
        async def replenish(_): actual['availableQuantity'] = '105'
        browser.replenish_ad = AsyncMock(side_effect=replenish)
        returned = ReturnFunds(self.journal, state, asyncio.Event(), p1, p2, browser)
        return returned, state, p1, p2, browser, deposit, actual

    async def test_full_return_is_idempotent_and_preserves_verification(self):
        returned, state, p1, p2, browser, _, _ = self.setup_return()
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA', 'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40, 'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            await returned.run()
            await returned.run()
        self.assertEqual(state['pending_return']['stage'], 'done')
        p2.transfer_usdt.assert_awaited_once_with('OTC', 'SPOT', '100')
        p2.withdraw_usdt.assert_awaited_once()
        p1.transfer_usdt.assert_awaited_once_with('SPOT', 'OTC', '100')
        browser.replenish_ad.assert_awaited_once()
        self.assertEqual(state['pending_return']['refill_plan']['over_verify'], '{"types":[1]}')

    async def test_mexc_internal_return_with_network_suffix(self):
        returned, state, p1, p2, browser, deposit, _ = self.setup_return()
        deposit.update(coin='USDT-PLASMA', network='PLASMA')
        original_list = p2.wallet_list.side_effect

        async def internal_list(path, params=None):
            rows = await original_list(path, params)
            if path.endswith('withdraw/history'):
                rows[0].update(coin='USDT-PLASMA', transferType=1)
            return rows

        p2.wallet_list.side_effect = internal_list
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA',
                'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40,
                'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            await returned.run()
            await returned.run()
        self.assertEqual(state['pending_return']['stage'], 'done')
        p2.withdraw_usdt.assert_awaited_once()
        p1.transfer_usdt.assert_awaited_once()
        browser.replenish_ad.assert_awaited_once()

    async def test_wrong_network_coin_suffix_stops_return(self):
        returned, _, p1, p2, browser, _, _ = self.setup_return()
        original_list = p2.wallet_list.side_effect

        async def wrong_list(path, params=None):
            rows = await original_list(path, params)
            if path.endswith('withdraw/history'):
                rows[0]['coin'] = 'USDT-BSC'
            return rows

        p2.wallet_list.side_effect = wrong_list
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA',
                'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40,
                'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            with self.assertRaisesRegex(Paused, 'Данные вывода'):
                await returned.run()
        p2.withdraw_usdt.assert_awaited_once()
        p1.transfer_usdt.assert_not_awaited()
        browser.replenish_ad.assert_not_awaited()

    async def test_random_selection_uses_configured_bep20_address_and_bsc_api_name(self):
        returned, state, p1, p2, _, _, _ = self.setup_return()
        plasma = {'netWork': 'PLASMA', 'depositEnable': True, 'withdrawEnable': True,
                  'contract': 'PLASMA-CONTRACT', 'withdrawFee': '0', 'withdrawMin': '10',
                  'withdrawMax': '100000', 'withdrawIntegerMultiple': '6'}
        bsc = {**plasma, 'netWork': 'BSC', 'contract': 'BSC-CONTRACT', 'withdrawFee': '0.01',
               'withdrawIntegerMultiple': '18'}
        coins = [{'coin': 'USDT', 'networkList': [plasma, bsc]}]
        async def p1_list(path, params=None):
            if path.endswith('getall'): return coins
            if path.endswith('address'):
                return [{'coin': 'USDT', 'network': 'PLASMA', 'address': '0x' + '1' * 40, 'memo': None},
                        {'coin': 'USDT', 'network': 'BNB Smart Chain(BEP20)', 'address': '0x' + '2' * 40, 'memo': None}]
            raise AssertionError(path)
        p1.wallet_list.side_effect = p1_list
        p2.wallet_list.side_effect = lambda path, params=None: coins
        env = {'ROLLOVER_NETWORKS': 'PLASMA,BEP20',
               'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40,
               'MEXC_P1_DEPOSIT_ADDRESS_BEP20': '0x' + '2' * 40,
               'MEXC_P1_DEPOSIT_MEMO_PLASMA': '', 'MEXC_P1_DEPOSIT_MEMO_BEP20': ''}
        with patch.dict(os.environ, env), patch('return_funds.random.choice', side_effect=lambda choices: choices[1]):
            await returned.plan()
        self.assertEqual(state['pending_return']['network'], 'BSC')
        self.assertEqual(state['pending_return']['network_label'], 'BEP20')
        self.assertEqual(state['pending_return']['address'], env['MEXC_P1_DEPOSIT_ADDRESS_BEP20'])
        self.assertEqual(state['pending_return']['withdraw_amount'], '99.990000000000000000')
        p2.transfer_usdt.assert_not_awaited()
        p2.withdraw_usdt.assert_not_awaited()

    async def test_configured_address_must_match_p1_mexc_before_transfer(self):
        returned, _, _, p2, _, _, _ = self.setup_return()
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA',
                'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '9' * 40}):
            with self.assertRaisesRegex(Paused, 'Адрес депозита П1'):
                await returned.plan()
        p2.transfer_usdt.assert_not_awaited()
        p2.withdraw_usdt.assert_not_awaited()

    async def test_selected_p1_uses_own_deposit_address_for_network_return(self):
        returned, state, p1, p2, browser, _, _ = self.setup_return()
        state['pending_return']['p1_profile'] = '2'
        returned = ReturnFunds(self.journal, state, asyncio.Event(), p1, p2, browser)
        address = '0x' + '1' * 40
        with patch.dict(os.environ, {
                'ROLLOVER_NETWORKS': 'PLASMA',
                'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '9' * 40,
                'MEXC_P2_2_DEPOSIT_ADDRESS_PLASMA': address,
                'MEXC_P2_2_DEPOSIT_MEMO_PLASMA': ''}):
            await returned.plan()
        self.assertEqual(state['pending_return']['address'], address)
        p2.transfer_usdt.assert_not_awaited()
        p2.withdraw_usdt.assert_not_awaited()

    async def test_lost_transfer_response_is_not_resent(self):
        returned, state, _, p2, browser, _, _ = self.setup_return()
        p2.transfer_usdt.side_effect = TimeoutError('lost')
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA', 'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40, 'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            with self.assertRaises(TimeoutError): await returned.run()
            self.assertEqual(state['pending_return']['stage'], 'p2_to_spot_intent')
            with self.assertRaisesRegex(Paused, 'потерян'): await returned.run()
        self.assertEqual(p2.transfer_usdt.await_count, 1)
        p2.withdraw_usdt.assert_not_awaited()
        browser.replenish_ad.assert_not_awaited()

    async def test_wrong_deposit_blocks_p1_transfer_and_refill(self):
        returned, state, p1, _, browser, deposit, _ = self.setup_return()
        deposit['address'] = '0x' + '2' * 40
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA', 'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40, 'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            with self.assertRaisesRegex(Paused, 'Адрес или сеть'): await returned.run()
        p1.transfer_usdt.assert_not_awaited()
        browser.replenish_ad.assert_not_awaited()

    async def test_lost_withdraw_response_does_not_resend(self):
        returned, state, p1, p2, browser, _, _ = self.setup_return()
        p2.withdraw_usdt.side_effect = TimeoutError('lost withdraw response')
        async def no_history(path, params=None):
            if path.endswith('getall'):
                return [{'coin': 'USDT', 'networkList': [{
                    'netWork': 'PLASMA', 'depositEnable': True, 'withdrawEnable': True,
                    'contract': 'CONTRACT', 'withdrawFee': '0', 'withdrawMin': '10',
                    'withdrawMax': '100000', 'withdrawIntegerMultiple': '6'}]}]
            if path.endswith('withdraw/history'): return []
            raise AssertionError(path)
        p2.wallet_list.side_effect = no_history
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA', 'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40, 'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            with self.assertRaises(TimeoutError): await returned.run()
            self.assertEqual(state['pending_return']['stage'], 'withdraw_intent')
            with self.assertRaisesRegex(Paused, 'withdrawOrderId'): await returned.run()
        self.assertEqual(p2.withdraw_usdt.await_count, 1)
        p1.transfer_usdt.assert_not_awaited()
        browser.replenish_ad.assert_not_awaited()

    async def test_unknown_refill_response_reconciles_without_second_post(self):
        returned, state, _, _, browser, _, actual = self.setup_return()
        async def applied_then_lost(_):
            actual['availableQuantity'] = '105'
            raise TimeoutError('response lost')
        browser.replenish_ad.side_effect = applied_then_lost
        with patch.dict(os.environ, {'ROLLOVER_NETWORKS': 'PLASMA', 'MEXC_P1_DEPOSIT_ADDRESS_PLASMA': '0x' + '1' * 40, 'MEXC_P1_DEPOSIT_MEMO_PLASMA': ''}):
            with self.assertRaises(TimeoutError): await returned.run()
            self.assertEqual(state['pending_return']['stage'], 'refill_intent')
            await returned.run()
        self.assertEqual(browser.replenish_ad.await_count, 1)
        self.assertEqual(state['pending_return']['stage'], 'done')
