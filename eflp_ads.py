"""Restore a maker's offline Eflp advertisement through its own browser."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import os

from adspower import AdsPower, AdsPowerClickUnknown, AdsPowerError, AdsPowerUnavailable


def maker_browser(profile, env) -> AdsPower:
    return AdsPower(env.get('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                    env.get('ADSPOWER_API_KEY', ''), profile.adspower_profile_id,
                    alternative_mexc_domains=True)


async def ensure_online(client, browser: AdsPower, ad: dict, ad_no: str,
                        side: str, fiat: str) -> dict:
    """Publish only a verified CLOSE ad; return a fresh public API snapshot."""
    if (ad.get('advNo') != ad_no or ad.get('side') != side
            or ad.get('coinName') != 'USDT' or ad.get('fiatUnit') != fiat):
        raise AdsPowerError(f'Объявление {ad_no} не соответствует П1, стороне или валюте')
    status = ad.get('advStatus')
    if status == 'OPEN':
        return ad
    if status != 'CLOSE':
        raise AdsPowerError(f'Объявление {ad_no}: статус {status}; автоматическая публикация недоступна')
    await browser.ensure_started()
    await browser.ensure_mexc_page()
    await browser.publish_ad(ad_no, side, fiat)
    for attempt in range(5):
        current = await client.get_ad(ad_no)
        if (current.get('advNo') != ad_no or current.get('side') != side
                or current.get('coinName') != 'USDT' or current.get('fiatUnit') != fiat):
            raise AdsPowerError(f'Объявление {ad_no} изменилось после публикации')
        if current.get('advStatus') == 'OPEN':
            return current
        if attempt < 4:
            await asyncio.sleep(2)
    raise AdsPowerUnavailable(f'Публикация объявления {ad_no} ещё не подтверждена MEXC; статус будет проверен снова')


async def prepare_next_sell(journal, cycle_id: str, next_profile: str,
                            env=os.environ) -> None:
    """Make a transferred maker's empty SELL ad usable before switching P1."""
    from mexc_client import MexcP2PClient
    from trade_profiles import profile_from_env, settings_for_profile

    cycle = journal.cycle(cycle_id)
    spec = cycle['spec']
    if (cycle['status'] != 'completed' or spec.get('scheduler_mode') != 'eflp_unique'
            or spec.get('reverse_p1_profile') != next_profile):
        raise AdsPowerError('Финальный переход к новому П1 не подтверждён в журнале')
    completed = journal.step(cycle_id, 'reverse_complete')
    if not completed or completed['status'] != 'done':
        raise AdsPowerError('Новый П1 ещё не получил USDT по финальному ордеру')
    quantity = Decimal(str(completed['result'].get('quantity', 'NaN')))
    if not quantity.is_finite() or quantity <= 0:
        raise AdsPowerError('В финальном ордере нет подтверждённого количества USDT')
    step_name = 'eflp_next_sell_refill'
    saved = journal.step(cycle_id, step_name)
    if saved and saved['status'] == 'done':
        return
    profile = profile_from_env(next_profile, env)
    settings = settings_for_profile(next_profile)
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                          settings.recv_window, settings.proxy_url)
    try:
        ad = await client.get_ad(profile.sell_adv_no)
        if (ad.get('advNo') != profile.sell_adv_no or ad.get('side') != 'SELL'
                or ad.get('coinName') != 'USDT' or not ad.get('fiatUnit')):
            raise AdsPowerError('SELL-объявление нового П1 не совпадает с настройками')
        if saved is None and Decimal(str(ad.get('availableQuantity', 'NaN'))) > 0:
            journal.transition(cycle_id, step_name, 'system', 'done',
                               'SELL нового П1 уже имеет доступный остаток',
                               result={'ad_no': profile.sell_adv_no, 'action': 'existing'})
            return
        browser = maker_browser(profile, env)
        await browser.ensure_started()
        await browser.ensure_mexc_page()
        actual = await browser.ad_details(profile.sell_adv_no)
        if (actual.get('id') != profile.sell_adv_no or actual.get('tradeType') != 1
                or actual.get('coinName') != 'USDT'
                or actual.get('currency') != ad['fiatUnit']):
            raise AdsPowerError('Браузер нового П1 показывает другое SELL-объявление')
        before = Decimal(str(actual.get('availableQuantity', 'NaN')))
        frozen = Decimal(str(actual.get('frozenQuantity', 'NaN')))
        api_before = Decimal(str(ad.get('availableQuantity', 'NaN')))
        if (not all(value.is_finite() and value >= 0 for value in (before, frozen, api_before))
                or before != api_before or frozen != 0):
            raise AdsPowerError('Остаток SELL-объявления нового П1 не совпадает с API или заморожен')
        if saved:
            plan = saved['result']
            if plan.get('adv_no') != profile.sell_adv_no:
                raise AdsPowerError('Сохранённый план SELL-объявления относится к другому П1')
            if before == Decimal(str(plan['target_available'])):
                journal.transition(cycle_id, step_name, 'system', 'done',
                                   'Пополнение SELL нового П1 подтверждено после повторного чтения',
                                   result=plan)
                return
            if saved['status'] == 'in_flight':
                raise AdsPowerUnavailable('Пополнение SELL нового П1 отправлялось, но итог не подтверждён; '
                                          'проверьте остаток на MEXC перед повтором')
            if saved['status'] != 'rejected' or before != Decimal(str(plan['before_available'])):
                raise AdsPowerError('Остаток SELL нового П1 изменился после сохранения плана')
        else:
            if before != 0 or ad.get('advStatus') != 'CLOSE':
                raise AdsPowerError('Автопополнение нового П1 допустимо только для пустого закрытого SELL')
            plan = {'method': 'quantity_only', 'adv_no': profile.sell_adv_no,
                    'fiat': ad['fiatUnit'], 'side': 'SELL',
                    'quantity': str(quantity), 'before_available': str(before),
                    'target_available': str(before + quantity),
                    'over_verify': actual.get('overVerify')}
        journal.transition(cycle_id, step_name, 'system', 'in_flight',
                           'Пополнение SELL нового П1 подготовлено', result=plan)
        try:
            await browser.replenish_ad(plan)
        except AdsPowerError as exc:
            if not isinstance(exc, (AdsPowerUnavailable, AdsPowerClickUnknown)):
                journal.transition(cycle_id, step_name, 'system', 'rejected',
                                   'MEXC явно отклонил пополнение SELL нового П1', result=plan)
            raise
        for attempt in range(5):
            updated = await client.get_ad(profile.sell_adv_no)
            if Decimal(str(updated.get('availableQuantity', 'NaN'))) == before + quantity:
                journal.transition(cycle_id, step_name, 'system', 'done',
                                   'SELL нового П1 пополнен полученными USDT', result=plan)
                return
            if attempt < 4:
                await asyncio.sleep(2)
        raise AdsPowerUnavailable('Пополнение SELL нового П1 не подтверждено API MEXC; '
                                  'проверьте остаток перед повтором')
    finally:
        await client.close()
