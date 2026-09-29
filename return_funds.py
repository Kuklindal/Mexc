"""Restore only USDT actually bought in a cycle rejected at the reverse order.

Each money-moving request has a durable stage. Unknown outcomes never trigger a
second POST; a subsequent run checks the saved exchange ID or pauses for review.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_DOWN
import os
import random
import time
import uuid

from config import Settings
from cycle import CycleRunner, OperatorStopped, Paused, fingerprint
from mexc_client import MexcP2PClient
from rollover import save_state

NETWORK_NAMES = {'PLASMA': 'PLASMA', 'BEP20': 'BSC'}
NETWORK_ALIASES = {'PLASMA': {'PLASMA'},
                   'BSC': {'BSC', 'BEP20', 'BEP20(BSC)', 'BNB SMART CHAIN(BEP20)'}}


def same_network(value, name):
    return isinstance(value, str) and value.upper() in NETWORK_ALIASES[name]


def same_coin(value, network_name):
    """MEXC may suffix USDT with the network in wallet history."""
    return value in {'USDT', f'USDT-{network_name}'}


def configured_networks():
    raw = os.getenv('ROLLOVER_NETWORKS') or os.getenv('ROLLOVER_NETWORK', 'PLASMA')
    names = [name.strip().upper() for name in raw.split(',')]
    if not names or any(name not in NETWORK_NAMES for name in names) or len(set(names)) != len(names):
        raise Paused('ROLLOVER_NETWORKS: укажите PLASMA, BEP20 или обе сети без повторов')
    return names


def amount(value):
    try:
        result = Decimal(str(value))
    except Exception:
        raise Paused('Некорректное количество USDT в ответе MEXC') from None
    if not result.is_finite() or result < 0:
        raise Paused('Некорректное количество USDT в ответе MEXC')
    return result


def text(value):
    return format(value, 'f')


async def wait(stop_event):
    if stop_event.is_set():
        raise OperatorStopped('Остановка перед следующим действием; прогресс сохранён')
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass
    if stop_event.is_set():
        raise OperatorStopped('Остановка перед следующим действием; прогресс сохранён')


async def network(client, name):
    rows = await client.wallet_list('/api/v3/capital/config/getall')
    hits = [net for coin in rows if coin.get('coin') == 'USDT'
            for net in coin.get('networkList', []) if net.get('netWork') == name]
    if len(hits) != 1:
        raise Paused(f'Сеть {name} для USDT не найдена однозначно')
    return hits[0]


async def destination(client, name, pinned):
    if not pinned:
        raise Paused(f'Адрес депозита П1 для {name} не задан в .env')
    rows = await client.wallet_list('/api/v3/capital/deposit/address', {'coin': 'USDT'})
    hits = [row for row in rows if row.get('coin') == 'USDT' and same_network(row.get('network'), name)
            and row.get('address') == pinned]
    if len(hits) != 1:
        raise Paused('Адрес депозита П1 в выбранной сети не найден однозначно')
    found = {'address': hits[0]['address'], 'memo': hits[0].get('memo') or ''}
    return found


def match_transfer(detail, tran_id, source, target, quantity):
    if (detail.get('tranId') != tran_id or detail.get('asset') != 'USDT'
            or detail.get('fromAccountType') != source or detail.get('toAccountType') != target
            or amount(detail.get('amount')) != amount(quantity)):
        raise Paused('Данные внутреннего перевода не совпадают с планом')
    return detail.get('status')


class ReturnFunds:
    def __init__(self, journal, state, stop_event, p1, p2, browser):
        self.journal, self.state, self.stop = journal, state, stop_event
        self.p1, self.p2, self.browser = p1, p2, browser
        self.pending = state['pending_return']
        self.timeout = int(os.getenv('ROLLOVER_CONFIRM_TIMEOUT_SECONDS', '1800'))
        if not 10 <= self.timeout <= 86400:
            raise ValueError('ROLLOVER_CONFIRM_TIMEOUT_SECONDS должен быть от 10 до 86400')

    def save(self, stage, **values):
        self.pending.update(values, stage=stage)
        save_state(self.journal, self.state)

    async def plan(self):
        saved = self.pending
        if saved.get('stage'):
            return
        cycle = self.journal.cycle(saved['cycle_id'])
        sale = self.journal.step(saved['cycle_id'], 'forward_complete')
        if not sale or sale['status'] != 'done':
            raise Paused('Первая продажа не подтверждена; возврат средств остановлен')
        quantity = amount(sale['result']['quantity'])
        if quantity <= 0:
            raise Paused('Нулевой объём купленных USDT')
        candidates = []
        for label in configured_networks():
            pinned = os.getenv(f'MEXC_P1_DEPOSIT_ADDRESS_{label}', '').strip()
            if not pinned:
                continue
            name = NETWORK_NAMES[label]
            try:
                source, target = await network(self.p2, name), await network(self.p1, name)
            except Paused:
                continue
            if (source.get('withdrawEnable') is not True or target.get('depositEnable') is not True
                    or not source.get('contract') or source.get('contract') != target.get('contract')):
                continue
            destination_info = await destination(self.p1, name, pinned)
            if os.getenv(f'MEXC_P1_DEPOSIT_MEMO_{label}', '').strip() != destination_info['memo']:
                raise Paused(f'Memo депозита П1 для {label} не совпадает с настройкой')
            fee = amount(source.get('withdrawFee'))
            try:
                precision = int(source.get('withdrawIntegerMultiple'))
            except (TypeError, ValueError):
                continue
            if not 0 <= precision <= 18:
                continue
            withdrawal = (quantity - fee).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
            if withdrawal <= 0 or not amount(source.get('withdrawMin')) <= withdrawal <= amount(source.get('withdrawMax')):
                continue
            candidates.append((label, name, source, destination_info, fee, withdrawal))
        if not candidates:
            raise Paused('Нет доступной сети с указанным адресом П1. Заполните MEXC_P1_DEPOSIT_ADDRESS_PLASMA '
                         'или MEXC_P1_DEPOSIT_ADDRESS_BEP20 и проверьте сеть на MEXC')
        label, name, source, destination_info, fee, withdrawal = random.choice(candidates)
        self.save('planned', quantity=text(quantity), fee=text(fee), withdraw_amount=text(withdrawal),
                  network=name, network_label=label, address=destination_info['address'], memo=destination_info['memo'],
                  contract=source['contract'], request_id=uuid.uuid4().hex,
                  created_ms=int(time.time() * 1000) - 60000,
                  p1_key=fingerprint(self.p1.api_key), p2_key=fingerprint(self.p2.api_key),
                  adv_no=cycle['spec']['forward_adv_no'], fiat=cycle['spec']['fiat'])

    async def transfer(self, client, prefix, source, target, qty, next_stage):
        pending = self.pending
        stage = pending['stage']
        intent, submitted = prefix + '_intent', prefix + '_submitted'
        if stage not in {intent, submitted}:
            if self.stop.is_set():
                raise OperatorStopped('Остановлено до перевода')
            self.save(intent, **{prefix + '_started_ms': int(time.time() * 1000)})
            stage = intent
            tran_id = await client.transfer_usdt(source, target, qty)
            self.save(submitted, **{prefix + '_id': tran_id})
        elif stage == intent:
            raise Paused(f'Ответ на {prefix} потерян; проверьте историю MEXC. Повтор не отправлен')
        tran_id = pending[prefix + '_id']
        deadline = time.monotonic() + self.timeout
        while True:
            detail = await client.get_wallet_transfer(tran_id)
            status = match_transfer(detail, tran_id, source, target, qty)
            if status == 'SUCCESS':
                self.save(next_stage)
                return
            if status != 'WAIT' or time.monotonic() >= deadline:
                raise Paused(f'Перевод {prefix} не подтверждён: {status}; повтор не отправлен')
            await wait(self.stop)

    async def rows(self, client, endpoint):
        if self.pending['created_ms'] < (time.time() - 7 * 86400) * 1000:
            raise Paused('История старше 7 дней требует ручной сверки; автоматического повтора нет')
        return await client.wallet_list(endpoint, {'coin': 'USDT', 'startTime': self.pending['created_ms'], 'limit': 1000})

    async def withdrawal(self):
        p = self.pending
        stage = p['stage']
        if stage == 'p2_spot':
            current = await network(self.p2, p['network'])
            address = await destination(self.p1, p['network'], p['address'])
            label = p.get('network_label', p['network'])
            if (os.getenv(f'MEXC_P1_DEPOSIT_ADDRESS_{label}', '').strip() != p['address']
                    or os.getenv(f'MEXC_P1_DEPOSIT_MEMO_{label}', '').strip() != p['memo']):
                raise Paused('Адрес или memo П1 в .env изменились после выбора сети; вывод остановлен')
            if (current.get('withdrawEnable') is not True or current.get('contract') != p['contract']
                    or amount(current.get('withdrawFee')) != amount(p['fee'])
                    or address['memo'] != p['memo']):
                raise Paused('Условия вывода или адрес депозита изменились')
            if self.stop.is_set():
                raise OperatorStopped('Остановлено до вывода')
            self.save('withdraw_intent')
            ident = await self.p2.withdraw_usdt(p['withdraw_amount'], p['network'], p['address'],
                                                 p['memo'], p['request_id'])
            self.save('withdraw_submitted', withdraw_id=ident)
        deadline = time.monotonic() + self.timeout
        while True:
            rows = await self.rows(self.p2, '/api/v3/capital/withdraw/history')
            hits = [row for row in rows if row.get('withdrawOrderId') == p['request_id']]
            if len(hits) > 1:
                raise Paused('Несколько выводов с одним withdrawOrderId')
            if hits:
                row = hits[0]
                if (p.get('withdraw_id') and str(row.get('id')) != p['withdraw_id']
                        or not same_coin(row.get('coin'), p['network']) or row.get('address') != p['address']
                        or (row.get('memo') or '') != p['memo']
                        or not same_network(row.get('network', p['network']), p['network'])
                        or amount(row.get('amount')) != amount(p['withdraw_amount'])):
                    raise Paused('Данные вывода не совпали с сохранённым планом')
                self.save('withdraw_submitted', withdraw_id=str(row['id']))
                if row.get('status') == 7:
                    if row.get('transferType') not in {0, 1} or not row.get('txId'):
                        raise Paused('Нет подтверждённого идентификатора вывода')
                    fee = amount(row.get('transactionFee'))
                    if fee > amount(p['fee']):
                        raise Paused('Фактическая комиссия превысила резерв')
                    self.save('withdrawn', tx_id=row['txId'], actual_fee=text(fee))
                    return
                if row.get('status') in {8, 9, 10}:
                    raise Paused('Вывод отклонён, отменён или требует ручной проверки')
            elif p['stage'] == 'withdraw_intent':
                raise Paused('Результат запроса вывода неизвестен; повтор запрещён. Сверьте withdrawOrderId в MEXC')
            if time.monotonic() >= deadline:
                raise Paused('Вывод ещё не подтверждён; продолжение сверит тот же withdrawOrderId')
            await wait(self.stop)

    async def deposit(self):
        p = self.pending
        deadline = time.monotonic() + self.timeout
        while True:
            rows = await self.rows(self.p1, '/api/v3/capital/deposit/hisrec')
            hits = [row for row in rows if row.get('txId') == p['tx_id']]
            if len(hits) > 1:
                raise Paused('Депозит П1 по txId не найден однозначно')
            if hits:
                row = hits[0]
                if (not same_coin(row.get('coin'), p['network']) or not same_network(row.get('network'), p['network'])
                        or row.get('address') != p['address'] or (row.get('memo') or row.get('addressTag') or '') != p['memo']):
                    raise Paused('Адрес или сеть депозита П1 не совпали с выводом')
                if row.get('status') in {5, 12}:
                    credited = amount(row.get('amount'))
                    requested = amount(p['withdraw_amount'])
                    if credited <= 0 or credited not in {requested, requested - amount(p['actual_fee'])}:
                        raise Paused('Количество зачисленных USDT не соответствует выводу')
                    self.save('deposited', credited=text(credited))
                    return
                if row.get('status') in {7, 8, 10, 11}:
                    raise Paused('Депозит П1 отклонён или ограничен')
            if time.monotonic() >= deadline:
                raise Paused('Депозит П1 ещё не подтверждён; продолжение проверит тот же txId')
            await wait(self.stop)

    async def replenish(self):
        p = self.pending
        proxy = type('PlanContext', (), {'browser': self.browser, 'spec': {'fiat': p['fiat']}})()
        if p['stage'] == 'p1_otc':
            ad = await self.p1.get_ad(p['adv_no'])
            plan = await CycleRunner.quantity_plan(proxy, ad, p['adv_no'], p['credited'])
            self.save('refill_intent', refill_plan=plan)
            if self.stop.is_set():
                raise OperatorStopped('Остановлено до пополнения')
            await self.browser.replenish_ad(plan)
            await CycleRunner.check_browser_ad(proxy, plan)
            self.save('refilled')
        elif p['stage'] == 'refill_intent':
            await CycleRunner.check_browser_ad(proxy, p['refill_plan'])
            self.save('refilled')

    async def run(self):
        await self.plan()
        p = self.pending
        if fingerprint(self.p1.api_key) != p['p1_key'] or fingerprint(self.p2.api_key) != p['p2_key']:
            raise Paused('API-профиль изменился во время возврата USDT')
        if p['stage'] in {'planned', 'p2_to_spot_intent', 'p2_to_spot_submitted'}:
            await self.transfer(self.p2, 'p2_to_spot', 'OTC', 'SPOT', p['quantity'], 'p2_spot')
        if p['stage'] in {'p2_spot', 'withdraw_intent', 'withdraw_submitted'}:
            await self.withdrawal()
        if p['stage'] == 'withdrawn':
            await self.deposit()
        if p['stage'] in {'deposited', 'p1_to_otc_intent', 'p1_to_otc_submitted'}:
            await self.transfer(self.p1, 'p1_to_otc', 'SPOT', 'OTC', p['credited'], 'p1_otc')
        if p['stage'] in {'p1_otc', 'refill_intent'}:
            await self.replenish()
        if p['stage'] == 'refilled':
            self.save('done')
        if p['stage'] != 'done':
            raise Paused(f"Незавершённый этап возврата: {p['stage']}")


async def finish_return(journal, state, stop_event):
    from adspower import AdsPower
    pending = state['pending_return']
    p1_settings, p2_settings = Settings.from_env('p1'), Settings.from_env('p2', p2_profile=pending['profile'])
    p1 = MexcP2PClient(p1_settings.api_key, p1_settings.secret_key, p1_settings.base_url,
                       p1_settings.recv_window, proxy_url=p1_settings.proxy_url)
    p2 = MexcP2PClient(p2_settings.api_key, p2_settings.secret_key, p2_settings.base_url,
                       p2_settings.recv_window, proxy_url=p2_settings.proxy_url)
    try:
        await ReturnFunds(journal, state, stop_event, p1, p2, AdsPower.from_env()).run()
    finally:
        await p1.close()
        await p2.close()


async def bind_transfer(journal, state, tran_id):
    """Recover a lost internal-transfer response after matching MEXC history."""
    p = (state or {}).get('pending_return') or {}
    stage = p.get('stage')
    if stage == 'p2_to_spot_intent':
        actor, prefix, source, target, qty = 'p2', 'p2_to_spot', 'OTC', 'SPOT', p['quantity']
    elif stage == 'p1_to_otc_intent':
        actor, prefix, source, target, qty = 'p1', 'p1_to_otc', 'SPOT', 'OTC', p['credited']
    else:
        raise ValueError('Нет внутреннего перевода с потерянным ответом')
    if not isinstance(tran_id, str) or not 1 <= len(tran_id) <= 128 or not tran_id.isalnum():
        raise ValueError('Некорректный tranId')
    settings = Settings.from_env(actor, p2_profile=p['profile'] if actor == 'p2' else None)
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                           settings.recv_window, proxy_url=settings.proxy_url)
    try:
        expected_key = p['p2_key'] if actor == 'p2' else p['p1_key']
        if fingerprint(settings.api_key) != expected_key:
            raise Paused('API-профиль не совпал с сохранённым возвратом')
        detail = await client.get_wallet_transfer(tran_id)
        status = match_transfer(detail, tran_id, source, target, qty)
        started = p.get(prefix + '_started_ms')
        stamp = detail.get('timestamp')
        if not isinstance(stamp, (int, float)) or not isinstance(started, int) or not started - 60000 <= stamp <= started + 300000:
            raise Paused('Время перевода не соответствует сохранённой попытке')
        if status not in {'WAIT', 'SUCCESS'}:
            raise Paused('Перевод MEXC не принят или не найден')
        p['stage'] = prefix + '_submitted'
        p[prefix + '_id'] = tran_id
        save_state(journal, state)
    finally:
        await client.close()
