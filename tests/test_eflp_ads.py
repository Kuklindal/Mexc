import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from adspower import AdsPowerError, AdsPowerUnavailable
from cycle import CycleRunner, Step
from eflp_ads import ensure_online, prepare_next_sell
from journal import Journal
from mexc_client import MexcAPIError


AD_NO = 'a1835908380845933569'


def ad(status='CLOSE', number=AD_NO):
    return {'advNo': number, 'side': 'BUY', 'coinName': 'USDT',
            'fiatUnit': 'TJS', 'advStatus': status}


class EflpAdTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_maker_empty_sell_is_refilled_once_from_verified_transfer(self):
        with TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'cycles.sqlite3')
            cycle_id = journal.create({'scheduler_mode': 'eflp_unique',
                                       'reverse_p1_profile': 'p1_2'})
            journal.transition(cycle_id, 'reverse_complete', 'both', 'done', 'received',
                               result={'quantity': '101'}, cycle_status='completed')
            public = {'advNo': AD_NO, 'side': 'SELL', 'coinName': 'USDT',
                      'fiatUnit': 'KZT', 'advStatus': 'CLOSE',
                      'availableQuantity': '0'}
            client = MagicMock(get_ad=AsyncMock(side_effect=[public,
                {**public, 'availableQuantity': '101'}]), close=AsyncMock())
            browser = MagicMock(ensure_started=AsyncMock(), ensure_mexc_page=AsyncMock(),
                ad_details=AsyncMock(return_value={'id': AD_NO, 'tradeType': 1,
                    'coinName': 'USDT', 'currency': 'KZT', 'availableQuantity': 0,
                    'frozenQuantity': 0, 'overVerify': None}),
                replenish_ad=AsyncMock())
            profile = SimpleNamespace(sell_adv_no=AD_NO, adspower_profile_id='browser')
            settings = SimpleNamespace(api_key='key', secret_key='secret',
                base_url='https://api.mexc.com', recv_window=5000, proxy_url=None)
            with patch('trade_profiles.profile_from_env', return_value=profile), \
                 patch('trade_profiles.settings_for_profile', return_value=settings), \
                 patch('mexc_client.MexcP2PClient', return_value=client), \
                 patch('eflp_ads.maker_browser', return_value=browser):
                await prepare_next_sell(journal, cycle_id, 'p1_2')
                await prepare_next_sell(journal, cycle_id, 'p1_2')
            browser.replenish_ad.assert_awaited_once()
            self.assertEqual(browser.replenish_ad.await_args.args[0]['quantity'], '101')
            self.assertEqual(journal.step(cycle_id, 'eflp_next_sell_refill')['status'], 'done')
            journal.close()

    async def test_unknown_refill_is_reconciled_without_second_post(self):
        with TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'cycles.sqlite3')
            cycle_id = journal.create({'scheduler_mode': 'eflp_unique',
                                       'reverse_p1_profile': 'p1_2'})
            journal.transition(cycle_id, 'reverse_complete', 'both', 'done', 'received',
                               result={'quantity': '101'}, cycle_status='completed')
            journal.transition(cycle_id, 'eflp_next_sell_refill', 'system', 'in_flight',
                               'submitted', result={'adv_no': AD_NO,
                                   'before_available': '0', 'target_available': '101'})
            public = {'advNo': AD_NO, 'side': 'SELL', 'coinName': 'USDT',
                      'fiatUnit': 'KZT', 'advStatus': 'CLOSE',
                      'availableQuantity': '101'}
            client = MagicMock(get_ad=AsyncMock(return_value=public), close=AsyncMock())
            browser = MagicMock(ensure_started=AsyncMock(), ensure_mexc_page=AsyncMock(),
                ad_details=AsyncMock(return_value={'id': AD_NO, 'tradeType': 1,
                    'coinName': 'USDT', 'currency': 'KZT', 'availableQuantity': 101,
                    'frozenQuantity': 0}), replenish_ad=AsyncMock())
            profile = SimpleNamespace(sell_adv_no=AD_NO, adspower_profile_id='browser')
            settings = SimpleNamespace(api_key='key', secret_key='secret',
                base_url='https://api.mexc.com', recv_window=5000, proxy_url=None)
            with patch('trade_profiles.profile_from_env', return_value=profile), \
                 patch('trade_profiles.settings_for_profile', return_value=settings), \
                 patch('mexc_client.MexcP2PClient', return_value=client), \
                 patch('eflp_ads.maker_browser', return_value=browser):
                await prepare_next_sell(journal, cycle_id, 'p1_2')
            browser.replenish_ad.assert_not_awaited()
            self.assertEqual(journal.step(cycle_id, 'eflp_next_sell_refill')['status'], 'done')
            journal.close()

    async def test_unknown_refill_with_unchanged_balance_stays_blocked(self):
        with TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'cycles.sqlite3')
            cycle_id = journal.create({'scheduler_mode': 'eflp_unique',
                                       'reverse_p1_profile': 'p1_2'})
            journal.transition(cycle_id, 'reverse_complete', 'both', 'done', 'received',
                               result={'quantity': '101'}, cycle_status='completed')
            journal.transition(cycle_id, 'eflp_next_sell_refill', 'system', 'in_flight',
                               'submitted', result={'adv_no': AD_NO,
                                   'before_available': '0', 'target_available': '101'})
            public = {'advNo': AD_NO, 'side': 'SELL', 'coinName': 'USDT',
                      'fiatUnit': 'KZT', 'advStatus': 'CLOSE',
                      'availableQuantity': '0'}
            client = MagicMock(get_ad=AsyncMock(return_value=public), close=AsyncMock())
            browser = MagicMock(ensure_started=AsyncMock(), ensure_mexc_page=AsyncMock(),
                ad_details=AsyncMock(return_value={'id': AD_NO, 'tradeType': 1,
                    'coinName': 'USDT', 'currency': 'KZT', 'availableQuantity': 0,
                    'frozenQuantity': 0}), replenish_ad=AsyncMock())
            profile = SimpleNamespace(sell_adv_no=AD_NO, adspower_profile_id='browser')
            settings = SimpleNamespace(api_key='key', secret_key='secret',
                base_url='https://api.mexc.com', recv_window=5000, proxy_url=None)
            with patch('trade_profiles.profile_from_env', return_value=profile), \
                 patch('trade_profiles.settings_for_profile', return_value=settings), \
                 patch('mexc_client.MexcP2PClient', return_value=client), \
                 patch('eflp_ads.maker_browser', return_value=browser):
                with self.assertRaises(AdsPowerUnavailable):
                    await prepare_next_sell(journal, cycle_id, 'p1_2')
            browser.replenish_ad.assert_not_awaited()
            self.assertEqual(journal.step(cycle_id, 'eflp_next_sell_refill')['status'], 'in_flight')
            journal.close()

    async def test_reverse_replenish_validates_sell_ad_as_sell(self):
        runner = CycleRunner.__new__(CycleRunner)
        runner.spec = {'scheduler_mode': 'eflp_volume', 'fiat': 'TJS'}
        runner.clients = {'p1': MagicMock(get_ad=AsyncMock(return_value={
            **ad('OPEN'), 'side': 'SELL'}))}
        runner.browser = MagicMock()
        runner.reverse_browser = None
        result = await runner.ensure_eflp_ad_online(AD_NO, 'reverse', side='SELL')
        self.assertEqual(result['side'], 'SELL')

    async def test_offline_ad_is_published_before_trading(self):
        client = MagicMock(get_ad=AsyncMock(return_value=ad('OPEN')))
        browser = MagicMock(ensure_started=AsyncMock(), ensure_mexc_page=AsyncMock(),
                            publish_ad=AsyncMock())
        result = await ensure_online(client, browser, ad(), AD_NO, 'BUY', 'TJS')
        self.assertEqual(result['advStatus'], 'OPEN')
        browser.publish_ad.assert_awaited_once_with(AD_NO, 'BUY', 'TJS')

    async def test_online_ad_never_mutates(self):
        client = MagicMock()
        browser = MagicMock(publish_ad=AsyncMock())
        result = await ensure_online(client, browser, ad('OPEN'), AD_NO, 'BUY', 'TJS')
        self.assertEqual(result['advStatus'], 'OPEN')
        browser.publish_ad.assert_not_awaited()

    async def test_wrong_owner_never_publishes(self):
        browser = MagicMock(publish_ad=AsyncMock())
        with self.assertRaises(AdsPowerError):
            await ensure_online(MagicMock(), browser, ad(number='a1111111111111111'),
                                AD_NO, 'BUY', 'TJS')
        browser.publish_ad.assert_not_awaited()

    async def test_unconfirmed_publication_waits_for_reconciliation(self):
        client = MagicMock(get_ad=AsyncMock(return_value=ad('CLOSE')))
        browser = MagicMock(ensure_started=AsyncMock(), ensure_mexc_page=AsyncMock(),
                            publish_ad=AsyncMock())
        with patch('eflp_ads.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(AdsPowerUnavailable):
                await ensure_online(client, browser, ad(), AD_NO, 'BUY', 'TJS')
        browser.publish_ad.assert_awaited_once()

    async def test_p2_85010_retries_only_when_maker_ad_went_offline(self):
        runner = CycleRunner.__new__(CycleRunner)
        runner.spec = {'mode': 'api', 'scheduler_mode': 'eflp_volume'}
        runner.automatic = False
        p2 = MagicMock(create_order=AsyncMock(side_effect=[
            MexcAPIError('ad rejected', code=85010), 'd1838700195487711232']))
        p1 = MagicMock(get_ad=AsyncMock(return_value=ad('CLOSE')))
        runner.clients = {'p1': p1, 'p2': p2}
        runner.ensure_eflp_ad_online = AsyncMock()
        result = await runner.execute(Step('forward_create', 'p2', ''),
                                      {'adv_no': AD_NO}, recovery=False)
        self.assertEqual(result['order_no'], 'd1838700195487711232')
        self.assertEqual(p2.create_order.await_count, 2)
        self.assertEqual(runner.ensure_eflp_ad_online.await_count, 2)

    async def test_p2_85010_does_not_retry_an_online_ad(self):
        runner = CycleRunner.__new__(CycleRunner)
        runner.spec = {'mode': 'api', 'scheduler_mode': 'eflp_unique'}
        runner.automatic = False
        p2 = MagicMock(create_order=AsyncMock(side_effect=MexcAPIError('ad rejected', code=85010)))
        p1 = MagicMock(get_ad=AsyncMock(return_value=ad('OPEN')))
        runner.clients = {'p1': p1, 'p2': p2}
        runner.ensure_eflp_ad_online = AsyncMock()
        with self.assertRaises(MexcAPIError):
            await runner.execute(Step('forward_create', 'p2', ''),
                                 {'adv_no': AD_NO}, recovery=False)
        p2.create_order.assert_awaited_once()

    async def test_offline_ad_is_published_after_one_successful_replenishment(self):
        runner = CycleRunner.__new__(CycleRunner)
        runner.spec = {'mode': 'api', 'scheduler_mode': 'eflp_unique', 'fiat': 'TJS'}
        runner.automatic = False
        client = MagicMock(get_ad=AsyncMock(return_value=ad('CLOSE')))
        runner.clients = {'p1': client}
        runner.browser = MagicMock(replenish_ad=AsyncMock())
        runner.reverse_browser = None
        runner.maker_browser = None
        runner.snapshot = AsyncMock(return_value={'state': 'DONE'})
        runner.check_state = MagicMock()
        runner.quantity_plan = AsyncMock()
        runner.check_replenished = MagicMock()
        runner.check_browser_ad = AsyncMock()
        runner.ensure_eflp_ad_online = AsyncMock()
        plan = {'method': 'quantity_only', 'adv_no': AD_NO, 'quantity': '100',
                'side': 'SELL', 'fiat': 'TJS'}
        runner.quantity_plan.return_value = plan
        result = await runner.execute(Step('reverse_replenish', 'p1', ''),
                                      plan, recovery=False)
        self.assertEqual(result, plan)
        runner.browser.replenish_ad.assert_awaited_once_with(plan)
        runner.ensure_eflp_ad_online.assert_awaited_once_with(AD_NO, 'reverse', side='SELL')

    async def test_explicit_quantity_refusal_publishes_then_retries_once(self):
        runner = CycleRunner.__new__(CycleRunner)
        runner.spec = {'mode': 'api', 'scheduler_mode': 'eflp_volume', 'fiat': 'TJS'}
        runner.automatic = False
        client = MagicMock(get_ad=AsyncMock(side_effect=[
            ad('CLOSE'), ad('CLOSE'), ad('OPEN'), ad('OPEN'), ad('OPEN')]))
        runner.clients = {'p1': client}
        runner.browser = MagicMock(replenish_ad=AsyncMock(side_effect=[
            AdsPowerError('quantity_rejected'), {'id': AD_NO}]))
        runner.reverse_browser = None
        runner.maker_browser = None
        runner.snapshot = AsyncMock(return_value={'state': 'DONE'})
        runner.check_state = MagicMock()
        runner.quantity_plan = AsyncMock()
        runner.check_replenished = MagicMock()
        runner.check_browser_ad = AsyncMock()
        runner.ensure_eflp_ad_online = AsyncMock()
        plan = {'method': 'quantity_only', 'adv_no': AD_NO, 'quantity': '100',
                'side': 'BUY', 'fiat': 'TJS'}
        runner.quantity_plan.return_value = plan
        await runner.execute(Step('reverse_replenish_buy', 'p1', ''), plan, recovery=False)
        self.assertEqual(runner.browser.replenish_ad.await_count, 2)
        self.assertEqual(runner.ensure_eflp_ad_online.await_count, 2)
        self.assertTrue(all(call.kwargs.get('side') == 'BUY'
                            for call in runner.ensure_eflp_ad_online.await_args_list))

    async def test_recovery_of_sell_replenishment_checks_without_adding_again(self):
        runner = CycleRunner.__new__(CycleRunner)
        runner.spec = {'mode': 'api', 'scheduler_mode': 'eflp_volume', 'fiat': 'TJS'}
        runner.automatic = False
        runner.clients = {'p1': MagicMock(get_ad=AsyncMock(return_value={
            **ad('OPEN'), 'side': 'SELL'}))}
        runner.browser = MagicMock(replenish_ad=AsyncMock())
        runner.reverse_browser = None
        runner.maker_browser = None
        runner.snapshot = AsyncMock(return_value={'state': 'DONE'})
        runner.check_state = MagicMock()
        runner.check_replenished = MagicMock()
        runner.check_browser_ad = AsyncMock()
        runner.ensure_eflp_ad_online = AsyncMock()
        plan = {'method': 'quantity_only', 'adv_no': AD_NO, 'quantity': '100',
                'side': 'SELL', 'fiat': 'TJS'}
        await runner.execute(Step('reverse_replenish', 'p1', ''), plan, recovery=True)
        runner.browser.replenish_ad.assert_not_awaited()
        runner.ensure_eflp_ad_online.assert_awaited_once()
        self.assertEqual(runner.ensure_eflp_ad_online.await_args.kwargs['side'], 'SELL')
