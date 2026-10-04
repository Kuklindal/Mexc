"""Persistent scheduling for the P2-maker volume and unique-counterparty modes.

The old on-chain recovery path remains in rollover.py for existing saved runs.
New runs never invoke that path: each completed first leg is returned by P2P.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import os
import random

from config import Settings, p2_nickname, p2_profile_name
from cycle import CashVolumeLimitReached, OperatorStopped, Paused, run_command
from mexc_client import MexcAPIError, MexcP2PClient, MexcReadUnavailable
from adspower import AdsPowerUnavailable
from rollover import (STATE_KEY, amount_from_ad, cycle_rowid, load_state, profiles_from_env,
                      save_state, wait_until)
from trade_profiles import profile_from_env, profile_prefix, validate_unique_profiles
from volume_policy import (TARGET_USDT, CEILING_USDT, CASH_TARGET_USDT, CASH_CEILING_USDT,
                           cooldown_until, record_purchase, rolling_cash_purchases,
                           rolling_cash_retry_at, window_for)


def configured_mode_profiles(mode: str, env=os.environ) -> tuple[str | None, list[str]]:
    """Read ordered profile keys for Telegram modes with fixed P2 selection."""
    fields = {'cash_volume': ('CASH_VOLUME_P1_PROFILE', 'CASH_VOLUME_P2_PROFILES'),
              'eflp_volume': (None, 'EFLP_VOLUME_P2_PROFILES')}
    if mode not in fields:
        raise ValueError('Для этого режима профили выбираются в Telegram')
    p1_field, p2_field = fields[mode]
    raw = (env.get(p2_field) or '').strip()
    if not raw or any(not item.strip() for item in raw.split(',')):
        raise ValueError(f'{p2_field}: укажите ключи П2 через запятую, например default,2,3')
    profiles = [p2_profile_name(item) for item in raw.split(',')]
    if len(profiles) != len(set(profiles)):
        raise ValueError(f'{p2_field}: один профиль П2 указан несколько раз')
    known = set(profiles_from_env(env))
    unknown = [name for name in profiles if name not in known]
    if unknown:
        raise ValueError(f'{p2_field}: нет в ROLLOVER_PROFILES: ' + ', '.join(unknown))
    p1 = None
    if p1_field:
        raw_p1 = (env.get(p1_field) or '').strip()
        if not raw_p1:
            raise ValueError(f'{p1_field}: укажите p1 или ключ профиля из ROLLOVER_PROFILES')
        p1 = 'p1' if raw_p1.lower() == 'p1' else p2_profile_name(raw_p1)
        if p1 != 'p1' and p1 not in known:
            raise ValueError(f'{p1_field}: профиля {p1} нет в ROLLOVER_PROFILES')
        if p1 in profiles:
            raise ValueError('П1 не может одновременно входить в список П2')
    return p1, profiles


def begin_mode(journal, mode: str, *, p1_profile='p1', p2_profiles=None, env=os.environ):
    if mode not in {'volume', 'unique', 'cash_volume', 'eflp_volume', 'eflp_unique'}:
        raise ValueError('Неизвестный режим торговли')
    names = profiles_from_env(env)
    if 'p1' in names:
        raise ValueError('Ключ профиля П2 «p1» зарезервирован для основного аккаунта П1')
    if mode == 'volume':
        if p1_profile != 'p1':
            raise ValueError('В режиме «Объём» П1 — основной аккаунт')
        chosen = names
        configured = [profile_from_env('p1', env),
                      *(profile_from_env(name, env) for name in chosen)]
        if (len({item.member_id for item in configured}) != len(configured)
                or len({item.adspower_profile_id for item in configured}) != len(configured)):
            raise ValueError('Участники должны иметь разные MEMBER_ID и профили AdsPower')
    elif mode == 'cash_volume':
        chosen = list(p2_profiles or [])
        if not chosen or len(chosen) != len(set(chosen)) or any(name not in names for name in chosen):
            raise ValueError('Выберите хотя бы один настроенный профиль П2 без повторов')
        if p1_profile in chosen:
            raise ValueError('Выбранный П1 не может одновременно быть П2')
        p1 = profile_from_env(p1_profile, env)
        if not (env.get(f'{profile_prefix(p1_profile)}_BUY_ADV_NO') or '').strip():
            raise ValueError('У выбранного П1 не указано объявление покупки USDT (BUY_ADV_NO)')
        members = {p1.member_id}
        keys = {(env.get(f'{p1.prefix}_API_KEY') or '').strip()}
        for name in chosen:
            p2_prefix = profile_prefix(name)
            required = ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME', 'PAYMENT_ID')
            missing = [f'{p2_prefix}_{field}' for field in required
                       if not (env.get(f'{p2_prefix}_{field}') or '').strip()]
            if missing:
                raise ValueError('Для П2 не заполнено: ' + ', '.join(missing))
            member = env[f'{p2_prefix}_MEMBER_ID'].strip()
            key = env[f'{p2_prefix}_API_KEY'].strip()
            if member in members or key in keys:
                raise ValueError('П1 и П2 должны иметь разные MEMBER_ID и API-ключи')
            members.add(member)
            keys.add(key)
    elif mode in {'eflp_volume', 'eflp_unique'}:
        chosen = list(p2_profiles or [])
        minimum = 20 if mode == 'eflp_unique' else 1
        if (len(chosen) < minimum or len(chosen) != len(set(chosen))
                or any(name not in names for name in chosen) or p1_profile in chosen):
            raise ValueError(f'Выберите не менее {minimum} разных П2, не включая П1')
        p1 = profile_from_env(p1_profile, env)
        if not (env.get(f'{p1.prefix}_BUY_ADV_NO') or '').strip():
            raise ValueError('Для П1 нужны объявления продажи и покупки USDT')
        member_ids = {p1.member_id}
        api_keys = {env.get(f'{p1.prefix}_API_KEY', '').strip()}
        for name in chosen:
            prefix = profile_prefix(name)
            required = ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME', 'PAYMENT_ID')
            missing = [f'{prefix}_{field}' for field in required
                       if not (env.get(f'{prefix}_{field}') or '').strip()]
            if missing:
                raise ValueError('Для П2 Eflp не заполнено: ' + ', '.join(missing))
            member = env[f'{prefix}_MEMBER_ID'].strip()
            key = env[f'{prefix}_API_KEY'].strip()
            if member in member_ids or key in api_keys:
                raise ValueError('П1 и П2 Eflp должны иметь разные MEMBER_ID и API-ключи')
            member_ids.add(member)
            api_keys.add(key)
    else:
        chosen = list(p2_profiles or [])
        if any(name not in names for name in chosen):
            raise ValueError('Выбранного П2 нет в ROLLOVER_PROFILES')
        validate_unique_profiles(p1_profile, chosen, env)
    previous = load_state(journal)
    if previous and previous['status'] not in {'done', 'stopped'}:
        raise ValueError('Сначала остановите или продолжите сохранённую серию')
    journal.ensure_can_create()
    state = {
        'status': 'ready', 'mode': mode, 'p1_profile': p1_profile,
        'cash_policy': 'rolling24_p1' if mode == 'cash_volume' else None,
        'profiles': chosen, 'selected': None, 'cursor': 0,
        'completed_count': 0, 'active_cycle': None, 'pending_return': None,
        'cooldowns': {key: value for key, value in previous.get('cooldowns', {}).items()
                      if mode != 'cash_volume' or value.get('reason') not in {'volume', 'volume_rolling', 'order_cap'}}
                     if previous else {},
        'volume_windows': dict(previous.get('volume_windows', {})) if previous else {},
        'return_dust_usdt': dict(previous.get('return_dust_usdt', {})) if previous else {},
        'unique_done': [], 'ad_rejection_streaks': dict(previous.get('ad_rejection_streaks', {})) if previous else {},
        'eflp_volume_by_profile': {}, 'eflp_counted_cycles': [], 'eflp_done': [],
        'last_cycle_rowid': journal.db.execute('SELECT COALESCE(MAX(rowid),0) FROM cycles').fetchone()[0],
    }
    save_state(journal, state)
    return state


def _cooldown(state, profile, now, journal=None):
    saved = state['cooldowns'].get(profile)
    if saved and saved.get('reason') not in {'volume_rolling', 'order_cap'}:
        if saved.get('manual_block'):
            return None
        if datetime.fromisoformat(saved['until']) > now:
            return datetime.fromisoformat(saved['until'])
    if state['mode'] == 'cash_volume' and journal is not None:
        window = rolling_cash_purchases(journal, profile, now,
                    (os.getenv(f'{profile_prefix(profile)}_MEMBER_ID') or '').strip())
        if (Decimal(window['quantity']) >= CASH_TARGET_USDT
                or window.get('uncertain_until')):
            until = rolling_cash_retry_at(window)
            if until is None:
                raise Paused('Невозможно вычислить срок скользящего лимита П2')
            state['cooldowns'][profile] = {'until': until.isoformat(),
                                           'anchor': window['orders'][0]['at'] if window['orders'] else window['uncertain_until'],
                                           'reason': 'volume_rolling', 'manual_block': False}
            return until
        if saved and saved.get('reason') == 'volume_rolling':
            state['cooldowns'].pop(profile, None)
            saved = None
    if saved and (saved.get('manual_block') or datetime.fromisoformat(saved['until']) > now):
        return datetime.fromisoformat(saved['until']) if not saved.get('manual_block') else None
    if state['mode'] in {'volume', 'cash_volume'}:
        window = window_for(state, profile, now)
        if state['mode'] == 'volume' and Decimal(window['quantity']) >= TARGET_USDT:
            try:
                until = cooldown_until(window)
            except ValueError:
                return None
            if until > now:
                state['cooldowns'][profile] = {'until': until.isoformat(), 'anchor': window['third_order_at'],
                                               'reason': 'volume', 'manual_block': False}
                return until
    return False


def next_profile(state, now=None, journal=None):
    now = now or datetime.now(timezone.utc)
    names = state['profiles']
    if not names:
        return None, None
    waits = []
    for offset in range(len(names)):
        name = names[(state['cursor'] + offset) % len(names)]
        if (state['mode'] in {'unique', 'eflp_unique'} and name in state['unique_done']
                or state['mode'] == 'eflp_volume' and name in state['eflp_done']):
            continue
        deadline = _cooldown(state, name, now, journal)
        if deadline is False:
            return name, None
        if isinstance(deadline, datetime):
            waits.append(deadline)
    return None, min(waits) if waits else None


async def _ad(client, ad_no, side, fiat=None):
    ad = await client.get_ad(ad_no)
    if (ad.get('advNo') != ad_no or ad.get('side') != side or ad.get('coinName') != 'USDT'
            or ad.get('advStatus') != 'OPEN' or (fiat and ad.get('fiatUnit') != fiat)):
        raise Paused(f'Объявление {ad_no} не соответствует владельцу, стороне, валюте или статусу OPEN')
    return ad


def _fiat_amount(low_usdt, high_usdt, price, fiat):
    low_cents = int((low_usdt * price * 100).to_integral_value(rounding='ROUND_CEILING'))
    high_cents = int((high_usdt * price * 100).to_integral_value(rounding=ROUND_DOWN))
    if low_cents > high_cents:
        raise Paused('У объявлений нет пересечения лимитов для нового ордера')
    return f'{Decimal(random.randint(low_cents, high_cents)) / 100:.2f} {fiat}'


async def choose_mode_amount(mode, p1_key, p2_key, state, env=os.environ, journal=None):
    """Re-read the advertisements needed by this mode before every cycle."""
    p1 = profile_from_env(p1_key, env)
    eflp = mode in {'eflp_volume', 'eflp_unique'}
    p2 = None if eflp or mode == 'cash_volume' else profile_from_env(p2_key, env)
    p1_settings = (Settings.from_env('p1') if p1_key == 'p1'
                   else Settings.from_env('p2', p2_profile=p1_key))
    settings = [p1_settings]
    if not eflp and mode != 'cash_volume':
        settings.append(Settings.from_env('p2', p2_profile=p2_key))
    clients = [MexcP2PClient(s.api_key, s.secret_key, s.base_url, s.recv_window,
                            proxy_url=s.proxy_url) for s in settings]
    try:
        sell = await _ad(clients[0], p1.sell_adv_no, 'SELL')
        reverse = (await _ad(clients[1], p2.sell_adv_no, 'SELL', sell.get('fiatUnit'))
                   if p2 else None)
        buy = None
        if mode in {'cash_volume', 'eflp_volume', 'eflp_unique'}:
            buy_no = (env.get(f'{profile_prefix(p1_key)}_BUY_ADV_NO') or '').strip()
            if not buy_no:
                raise Paused('У П1 нет номера объявления покупки USDT')
            buy = await _ad(clients[0], buy_no, 'BUY', sell.get('fiatUnit'))
        fiat = sell.get('fiatUnit')
        if not fiat:
            raise Paused('У объявления П1 нет фиатной валюты')
        p1_price = Decimal(str(sell['price']))
        if not p1_price.is_finite() or p1_price <= 0:
            raise Paused('Некорректная цена объявления продажи П1')
        if reverse:
            p2_price = Decimal(str(reverse['price']))
            p2_max = Decimal(str(reverse['maxSingleTransAmount']))
            p2_min = Decimal(str(reverse['minSingleTransAmount']))
            if min(p2_price, p2_max) <= 0 or not all(
                    x.is_finite() for x in (p2_price, p2_max, p2_min)):
                raise Paused('Некорректная цена или лимиты объявления П2')
            reverse_max_usdt = (p2_max - Decimal('0.01')) / p2_price
            reverse_min_usdt = p2_min / p2_price
        else:
            reverse_max_usdt, reverse_min_usdt = Decimal('Infinity'), Decimal('0')
        buy_max_usdt, buy_min_usdt = Decimal('Infinity'), Decimal('0')
        if buy is not None:
            buy_price = Decimal(str(buy['price']))
            buy_max = Decimal(str(buy['maxSingleTransAmount']))
            buy_min = Decimal(str(buy['minSingleTransAmount']))
            buy_available = Decimal(str(buy['availableQuantity']))
            if (not all(x.is_finite() for x in (buy_price, buy_max, buy_min, buy_available))
                    or buy_price <= 0 or buy_max <= 0 or buy_available < 0):
                raise Paused('Некорректная цена, лимит или остаток объявления покупки П1')
            buy_max_usdt = min((buy_max - Decimal('0.01')) / buy_price, buy_available)
            buy_min_usdt = buy_min / buy_price
        if mode in {'volume', 'cash_volume', 'eflp_volume'}:
            _, high_fiat, _ = amount_from_ad(sell)
            high_usdt = min(high_fiat / p1_price, reverse_max_usdt, buy_max_usdt)
            p1_min_usdt = Decimal(str(sell['minSingleTransAmount'])) / p1_price
            low_usdt = max(high_usdt - Decimal(100), p1_min_usdt,
                           reverse_min_usdt, buy_min_usdt)
            if high_usdt < 200:
                raise Paused(f'🚨 Доступный новый ордер меньше 200 USDT ({high_usdt:.2f}); проверьте объявления')
        else:
            p1_min = Decimal(str(sell['minSingleTransAmount'])) / p1_price
            p1_max = min(Decimal(str(sell['maxSingleTransAmount'])) / p1_price,
                         Decimal(str(sell['availableQuantity'])))
            low_usdt = max(Decimal(50), p1_min, reverse_min_usdt, buy_min_usdt)
            high_usdt = min(Decimal(100), p1_max, reverse_max_usdt, buy_max_usdt)
        amount = _fiat_amount(low_usdt, high_usdt, p1_price, fiat)
        if mode in {'volume', 'cash_volume'}:
            projected = Decimal(amount.split()[0]) / p1_price
            window = (rolling_cash_purchases(journal, p2_key,
                      member_id=(env.get(f'{profile_prefix(p2_key)}_MEMBER_ID') or '').strip())
                      if mode == 'cash_volume' and journal is not None else window_for(state, p2_key))
            ceiling = CASH_CEILING_USDT if mode == 'cash_volume' else CEILING_USDT
            if mode == 'cash_volume':
                # Leave a small cushion for the fiat-to-USDT rounding and a
                # price change between reading the advertisement and creation.
                projected *= Decimal('1.005')
            if (mode == 'cash_volume' and (Decimal(window['quantity']) >= CASH_TARGET_USDT
                                          or window.get('uncertain_until'))
                    or Decimal(window['quantity']) + projected > ceiling):
                return None
        return amount
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def _record_forward(journal, state, cycle_id):
    if state['mode'] not in {'volume', 'cash_volume', 'eflp_volume', 'eflp_unique'}:
        return
    saved = journal.step(cycle_id, 'forward_complete')
    if not saved or saved['status'] != 'done':
        return
    if state['mode'] in {'eflp_volume', 'eflp_unique'}:
        counted = state.setdefault('eflp_counted_cycles', [])
        if cycle_id not in counted:
            counted.append(cycle_id)
            profile = journal.cycle(cycle_id)['spec']['p2_profile']
            amounts = state.setdefault('eflp_volume_by_profile', {})
            amounts[profile] = str(Decimal(amounts.get(profile, '0'))
                                   + Decimal(saved['result']['quantity']))
            save_state(journal, state)
        return
    row = journal.db.execute("SELECT time FROM events WHERE cycle_id=? AND step='forward_create' "
                             "AND status='done' ORDER BY id LIMIT 1", (cycle_id,)).fetchone()
    if not row:
        row = journal.db.execute("SELECT time FROM events WHERE cycle_id=? AND step='forward_complete' "
                                 "AND status='done' ORDER BY id LIMIT 1", (cycle_id,)).fetchone()
    at = datetime.fromisoformat(row['time']) if row else datetime.now(timezone.utc)
    profile = journal.cycle(cycle_id)['spec']['p2_profile']
    record_purchase(state, profile, cycle_id, saved['result']['quantity'], at)
    save_state(journal, state)


def _defer_cash_limit(journal, state, profile, cycle_id=None):
    """Move to another P2 only when no first order was submitted."""
    if cycle_id:
        if journal.step(cycle_id, 'forward_create') or journal.step(cycle_id, 'forward_complete'):
            raise Paused('Есть попытка создания ордера П2; смена профиля требует сверки')
        journal.transition(cycle_id, 'cycle', 'system', 'abandoned',
                           'Новый ордер не отправлен: скользящий лимит П2',
                           cycle_status='abandoned')
        state['active_cycle'] = None
        state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    window = rolling_cash_purchases(journal, profile,
        member_id=(os.getenv(f'{profile_prefix(profile)}_MEMBER_ID') or '').strip())
    until = rolling_cash_retry_at(window)
    if until is None:
        raise Paused('Следующий ордер превышает лимит, но в журнале нет покупок П2; нужна сверка')
    state['cooldowns'][profile] = {'until': until.isoformat(),
        'anchor': window['orders'][0]['at'] if window['orders'] else window['uncertain_until'],
        'reason': 'order_cap', 'manual_block': False}
    state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
    state.pop('current_profile', None)
    save_state(journal, state)


def _finish_completed_cycle(journal, state, cycle_id, profile):
    spec = journal.cycle(cycle_id)['spec']
    if (journal.cycle(cycle_id)['status'] != 'completed'
            or not (returned := journal.step(cycle_id, 'reverse_complete'))
            or returned['status'] != 'done'
            or not (refilled := journal.step(cycle_id, 'reverse_replenish'))
            or refilled['status'] != 'done'):
        raise Paused('Цикл помечен завершённым без подтверждённого возврата и пополнения')
    buy_refilled = journal.step(cycle_id, 'reverse_replenish_buy')
    if spec.get('buy_replenish') and (not buy_refilled or buy_refilled['status'] != 'done'):
        raise Paused('Объявление покупки П1 не пополнено после обычного цикла')
    _record_forward(journal, state, cycle_id)
    state['completed_count'] += 1
    if spec.get('cash_forced_return_code') != 85010:
        state['ad_rejection_streaks'][profile] = 0
    residual = Decimal(str(returned['result'].get('residual_usdt', '0')))
    if residual > 0:
        dust = state.setdefault('return_dust_usdt', {})
        dust[profile] = str(Decimal(dust.get(profile, '0')) + residual)
    cash_rolling = state['mode'] == 'cash_volume' and spec.get('cash_policy') == 'rolling24_p1'
    cash_window = (rolling_cash_purchases(journal, profile,
                   member_id=spec.get('members', {}).get('p2')) if cash_rolling else None)
    cash_done = cash_rolling and Decimal(cash_window['quantity']) >= CASH_TARGET_USDT
    if state['mode'] == 'volume' or (state['mode'] == 'cash_volume' and spec.get('reverse_maker') == 'p2'):
        state['last_completed'] = {'cycle_id': cycle_id, 'profile': profile,
                                   'quantity': returned['result']['quantity']}
    elif cash_done:
        state['last_completed'] = {'cycle_id': cycle_id, 'profile': profile,
                                   'quantity': returned['result']['quantity']}
    elif state['mode'] in {'unique', 'eflp_unique'}:
        if profile not in state['unique_done']:
            state['unique_done'].append(profile)
    state['active_cycle'] = None
    state.pop('current_profile', None)
    state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    if (state['mode'] in {'unique', 'eflp_unique'}
            or state['mode'] == 'eflp_volume'
            and Decimal(state['eflp_volume_by_profile'].get(profile, '0')) >= Decimal('20000')
            or state['mode'] == 'cash_volume' and spec.get('reverse_maker') == 'p2'
            or cash_done):
        state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
    if (state['mode'] == 'eflp_volume'
            and Decimal(state['eflp_volume_by_profile'].get(profile, '0')) >= Decimal('20000')
            and profile not in state['eflp_done']):
        state['eflp_done'].append(profile)
        journal.transition(cycle_id, 'eflp_profile_done', 'system', 'done',
            f'✅ П2 {p2_nickname(profile)} выполнил объём Eflp: '
            f'{state["eflp_volume_by_profile"][profile]} USDT.')
    if state['mode'] == 'cash_volume' and spec.get('reverse_maker') == 'p2':
        forced = spec.get('cash_forced_cooldown')
        if forced:
            state['cooldowns'][profile] = forced
        else:
            anchor = spec.get('cash_third_order_at')
            if not anchor:
                raise Paused('У завершающего цикла не сохранена третья сделка для 24-часового таймера')
            until = datetime.fromisoformat(anchor) + timedelta(days=1)
            state['cooldowns'][profile] = {'until': until.isoformat(),
                                           'anchor': anchor,
                                           'reason': 'volume', 'manual_block': False}
    elif cash_done:
        until = rolling_cash_retry_at(cash_window)
        state['cooldowns'][profile] = {'until': until.isoformat(),
                                       'anchor': cash_window['orders'][0]['at'],
                                       'reason': 'volume_rolling', 'manual_block': False}
    save_state(journal, state)


async def _finish_cash_network_return(journal, state, cycle_id, profile, stop_event):
    """Resume the old wallet route before rotating P2; never resend an unknown POST."""
    from return_funds import finish_return

    spec = journal.cycle(cycle_id)['spec']
    sale = journal.step(cycle_id, 'forward_complete')
    if (spec.get('cash_return_route') != 'network' or not spec.get('cash_route_selected')
            or not sale or sale['status'] != 'done'):
        raise Paused('Вывод через сеть не подтверждён сохранённым циклом')
    pending = state.get('pending_return')
    if pending is None:
        pending = {'cycle_id': cycle_id, 'profile': profile,
                   'p1_profile': spec.get('p1_profile', 'p1')}
        state['pending_return'] = pending
        save_state(journal, state)
    elif (pending.get('cycle_id') != cycle_id or pending.get('profile') != profile
          or pending.get('p1_profile', 'p1') != spec.get('p1_profile', 'p1')):
        raise Paused('Сохранённый возврат относится к другому циклу или участнику')
    if pending.get('stage') != 'done':
        await finish_return(journal, state, stop_event)
    pending = state['pending_return']
    if pending.get('stage') != 'done' or not pending.get('credited'):
        raise Paused('Зачисление USDT П1 после вывода через сеть не подтверждено')
    credited = pending['credited']
    cooldown = spec.get('cash_forced_cooldown')
    if not cooldown:
        anchor = spec.get('cash_third_order_at')
        if not anchor:
            raise Paused('У завершающего цикла не сохранена третья сделка для 24-часового таймера')
        until = datetime.fromisoformat(anchor) + timedelta(days=1)
        cooldown = {'until': until.isoformat(), 'anchor': anchor,
                    'reason': 'volume', 'manual_block': False}
    saved_return = journal.step(cycle_id, 'cash_network_return')
    if saved_return and saved_return['status'] != 'done':
        raise Paused('В журнале нет подтверждённого завершения сетевого возврата')
    if saved_return and saved_return['result'] != {
            'credited': credited, 'network': pending['network'], 'tx_id': pending['tx_id']}:
        raise Paused('Сохранённые данные сетевого возврата расходятся с подтверждённым зачислением')
    if not saved_return:
        journal.transition(cycle_id, 'cash_network_return', 'system', 'done',
                           f'Вывод через сеть завершён; П1 получил {credited} USDT',
                           result={'credited': credited, 'network': pending['network'],
                                   'tx_id': pending['tx_id']})
    if journal.cycle(cycle_id)['status'] != 'completed':
        journal.transition(cycle_id, 'cycle', 'both', 'completed',
                           'Цикл завершён после подтверждённого вывода через сеть',
                           cycle_status='completed')
    _record_forward(journal, state, cycle_id)
    state['completed_count'] += 1
    if spec.get('cash_forced_return_code') != 85010:
        state['ad_rejection_streaks'][profile] = 0
    state['last_completed'] = {'cycle_id': cycle_id, 'profile': profile, 'quantity': credited}
    state['active_cycle'] = None
    state['pending_return'] = None
    state.pop('current_profile', None)
    state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
    state['cooldowns'][profile] = cooldown
    save_state(journal, state)


def _handle_forward_rejection(journal, state, cycle_id, profile):
    step = journal.step(cycle_id, 'forward_create')
    code = step['result'].get('rejected_code') if step and step['status'] == 'rejected' else None
    if code not in {60085, 85010}:
        return False
    # A confirmed explicit rejection created no order. Never abandon a cycle
    # with a confirmed first sale or an uncertain creation result.
    if journal.step(cycle_id, 'forward_complete') or step['result'].get('order_no'):
        raise Paused('У отклонённой первой сделки найден ордер; нужна ручная сверка')
    now = datetime.now(timezone.utc)
    manual_block = False
    if code == 60085 and state['mode'] in {'volume', 'cash_volume'}:
        window = window_for(state, profile, now)
        third = window.get('third_order_at')
        until = datetime.fromisoformat(third) + timedelta(days=1) if third else now + timedelta(days=1)
        manual_block = until <= now
    else:
        until = now + timedelta(days=1)
    state['cooldowns'][profile] = {'until': until.isoformat(), 'anchor': now.isoformat(),
                                   'reason': str(code), 'manual_block': manual_block}
    if code == 85010:
        streaks = state['ad_rejection_streaks']
        streaks[profile] = streaks.get(profile, 0) + 1
        if streaks[profile] == 3:
            journal.transition(cycle_id, 'ad_rejection_alert', 'system', 'done',
                f'🚨 MEXC 85010 три раза подряд у П2 {p2_nickname(profile)}; проверьте аккаунт.')
    else:
        state['ad_rejection_streaks'][profile] = 0
    journal.transition(cycle_id, 'rollover', 'system', 'abandoned',
                       f'Первая сделка явно отклонена MEXC ({code}); ордер не создан. П2 на таймере.',
                       cycle_status='abandoned')
    state['active_cycle'] = None
    state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
    save_state(journal, state)
    return True


def _switch_rejected_cash_reverse(journal, state, cycle_id, profile):
    """Switch only after an explicit exchange rejection with no reverse order."""
    if state['mode'] != 'cash_volume':
        return False
    cycle = journal.cycle(cycle_id)
    spec = cycle['spec']
    rejected = journal.step(cycle_id, 'reverse_create')
    if (spec.get('reverse_maker') != 'p1' or not rejected
            or rejected['status'] != 'rejected'
            or rejected['result'].get('rejected_code') not in {60085, 85010}
            or rejected['result'].get('order_no')
            or not journal.step(cycle_id, 'forward_complete')):
        return False
    if any(journal.step(cycle_id, key) for key in
           ('reverse_verify', 'reverse_message', 'reverse_paid', 'reverse_release', 'reverse_complete')):
        raise Paused('Обратный ордер имеет последующие шаги; смена объявления заблокирована')
    if spec.get('cash_policy') == 'rolling24_p1':
        raise Paused('MEXC отклонил обратный ордер в объявление П1. USDT остаются у П2; '
                     'проверьте ордер и объявление П1 перед продолжением.')
    code = rejected['result']['rejected_code']
    now = datetime.now(timezone.utc)
    third = spec.get('cash_third_order_at')
    if code == 60085 and third:
        until = datetime.fromisoformat(third) + timedelta(days=1)
    else:
        until = now + timedelta(days=1)
    cooldown = {'until': until.isoformat(), 'anchor': third or now.isoformat(),
                'reason': str(code), 'manual_block': code == 60085 and until <= now}
    network = spec.get('cash_final_return', 'p2p') == 'network'
    updated = dict(spec, reverse_maker='p1' if network else 'p2',
                   reverse_adv_no=spec['cash_p1_buy_adv_no'] if network else spec['cash_p2_sell_adv_no'],
                   cash_return_route='network' if network else 'p2p',
                   buy_replenish=False, cash_forced_return_code=code,
                   cash_forced_cooldown=cooldown)
    with journal.db:
        journal.db.execute('UPDATE cycles SET spec=? WHERE id=?', (json.dumps(updated), cycle_id))
        journal.db.execute("DELETE FROM steps WHERE cycle_id=? AND name IN ('reverse_ad','reverse_create')",
                           (cycle_id,))
    state['cooldowns'][profile] = cooldown
    if code == 85010:
        streaks = state['ad_rejection_streaks']
        streaks[profile] = streaks.get(profile, 0) + 1
        if streaks[profile] == 3:
            journal.transition(cycle_id, 'ad_rejection_alert', 'system', 'done',
                f'🚨 MEXC 85010 три раза подряд у П2 {p2_nickname(profile)}; проверьте аккаунт.')
    else:
        state['ad_rejection_streaks'][profile] = 0
    journal.transition(cycle_id, 'cash_route_override', 'system', 'done',
        f'MEXC явно отклонил обратный ордер ({code}); '
        + ('USDT вернутся П1 через сеть. ' if network else 'П1 выкупит USDT через объявление П2. ')
        + 'Прежний ордер не создан и повторяться не будет.')
    save_state(journal, state)
    return True


async def run_mode(journal, state, stop_event, telegram, telegram_keyboard=None):
    from main import build_parser
    state['status'] = 'running'
    save_state(journal, state)
    try:
        while True:
            if stop_event.is_set():
                raise OperatorStopped('Остановлено оператором')
            cycle_id = state.get('active_cycle')
            if cycle_id:
                cycle = journal.cycle(cycle_id)
                profile = cycle['spec']['p2_profile']
                if (cycle['spec'].get('scheduler_mode') != state['mode']
                        or cycle['spec'].get('p1_profile') != state['p1_profile']
                        or profile not in state['profiles']):
                    raise Paused('Активный цикл не соответствует сохранённому режиму и участникам')
                state['current_profile'] = profile
                _record_forward(journal, state, cycle_id)
                if (state['mode'] == 'cash_volume'
                        and cycle['spec'].get('cash_return_route') == 'network'):
                    await _finish_cash_network_return(journal, state, cycle_id, profile, stop_event)
                    continue
                if cycle['status'] == 'completed':
                    _finish_completed_cycle(journal, state, cycle_id, profile)
                    if state['mode'] == 'eflp_volume' and telegram:
                        from sheets import Reporter
                        await Reporter(journal, telegram, None, keyboard=telegram_keyboard).flush()
                    continue
                args = build_parser().parse_args(['cycle', '--resume', cycle_id])
            else:
                missed = journal.db.execute('SELECT id FROM cycles WHERE rowid>? ORDER BY rowid',
                                            (state['last_cycle_rowid'],)).fetchall()
                if missed:
                    if len(missed) != 1:
                        raise Paused('Найдено несколько новых циклов; продолжение требует сверки')
                    recovered = journal.cycle(missed[0]['id'])
                    spec = recovered['spec']
                    if (recovered['status'] == 'abandoned' or not spec.get('automatic')
                            or (spec.get('reverse_maker') != 'p2'
                                and state['mode'] not in {'cash_volume', 'eflp_volume', 'eflp_unique'})
                            or spec.get('scheduler_mode') != state['mode']
                            or spec.get('p1_profile') != state['p1_profile']
                            or spec.get('p2_profile') not in state['profiles']
                            or spec.get('series', {}).get('count') != 1):
                        raise Paused('Найден посторонний цикл; запуск нового ордера остановлен')
                    state['active_cycle'] = missed[0]['id']
                    save_state(journal, state)
                    continue
                if state['mode'] == 'unique' and len(state['unique_done']) == len(state['profiles']):
                    state['status'] = 'done'
                    save_state(journal, state)
                    if telegram and getattr(telegram, 'enabled', False):
                        await telegram.send('✅ Уникальные: все выбранные П2 завершили по одному полному циклу.',
                                            reply_markup=telegram_keyboard() if telegram_keyboard else None)
                    return
                eflp_target_done = (state['mode'] == 'eflp_volume'
                                    and len(state['eflp_done']) == len(state['profiles']))
                eflp_unique_done = (state['mode'] == 'eflp_unique'
                                    and len(state['unique_done']) >= 20)
                if eflp_target_done or eflp_unique_done:
                    state['status'] = 'done'
                    save_state(journal, state)
                    if telegram and getattr(telegram, 'enabled', False):
                        p1_name = profile_from_env(state['p1_profile'], os.environ).nickname
                        message = (f'✅ Объём Eflp выполнен для всех выбранных П2 у П1 {p1_name}.' if eflp_target_done else
                                   f'✅ Уникальные Eflp выполнены: П1 {p1_name}, '
                                   f'{len(state["unique_done"])} разных П2.')
                        await telegram.send(message, reply_markup=telegram_keyboard() if telegram_keyboard else None)
                    return
                profile, deadline = next_profile(state, journal=journal)
                save_state(journal, state)
                completed = state.get('last_completed')
                if (state['mode'] in {'volume', 'cash_volume'} and completed
                        and not journal.step(completed['cycle_id'], 'volume_switch_notice')):
                    next_label = (f'Следующий П2: {p2_nickname(profile)}.' if profile else
                                  'Доступных П2 пока нет; серия ждёт таймер.')
                    journal.transition(completed['cycle_id'], 'volume_switch_notice', 'system', 'done',
                        f"✅ Возврат USDT завершён для {p2_nickname(completed['profile'])}: "
                        f"{completed['quantity']} USDT. {next_label}")
                    if telegram and getattr(telegram, 'enabled', False):
                        from sheets import Reporter
                        await Reporter(journal, telegram, None, keyboard=telegram_keyboard).flush()
                if profile is None:
                    if deadline is None:
                        raise Paused('Нет доступного П2; проверьте таймеры и настройки')
                    state['status'] = 'waiting'
                    state.pop('last_error', None)
                    save_state(journal, state)
                    await wait_until(deadline, stop_event)
                    state['status'] = 'running'
                    save_state(journal, state)
                    continue
                amount = await choose_mode_amount(state['mode'], state['p1_profile'], profile, state,
                                                  journal=journal)
                if amount is None:
                    if state['mode'] == 'cash_volume':
                        _defer_cash_limit(journal, state, profile)
                        continue
                    window = window_for(state, profile)
                    until = cooldown_until(window)
                    state['cooldowns'][profile] = {'until': until.isoformat(), 'anchor': window['third_order_at'],
                                                   'reason': 'volume', 'manual_block': False}
                    state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
                    save_state(journal, state)
                    continue
                state['current_profile'] = profile
                save_state(journal, state)
                args = build_parser().parse_args(['cycle', '--auto', '--reverse-maker',
                    'p1' if state['mode'] in {'cash_volume', 'eflp_volume', 'eflp_unique'} else 'p2',
                    '--scheduler-mode', state['mode'],
                    '--p1-profile', state['p1_profile'], '--p2-profile', profile,
                    '--amount', amount, '--count', '1'])
            before = journal.db.execute('SELECT COALESCE(MAX(rowid),0) FROM cycles').fetchone()[0]
            limit_skipped = False
            try:
                await run_command(args, stop_event=stop_event, use_lock=False,
                                  notify_prepare_errors=False, telegram_keyboard=telegram_keyboard)
            except CashVolumeLimitReached:
                limited = cycle_id
                if limited is None:
                    created = journal.db.execute('SELECT id FROM cycles WHERE rowid>? ORDER BY rowid',
                                                 (before,)).fetchall()
                    if len(created) != 1:
                        raise Paused('Не удалось определить цикл без отправленного ордера')
                    limited = created[0]['id']
                _defer_cash_limit(journal, state, profile, limited)
                limit_skipped = True
                continue
            except (MexcReadUnavailable, AdsPowerUnavailable) as exc:
                state['status'] = 'waiting'
                state['last_error'] = str(exc)[:300]
                save_state(journal, state)
                await wait_until(datetime.now(timezone.utc) + timedelta(seconds=30), stop_event)
                state['status'] = 'running'
                save_state(journal, state)
                continue
            except MexcAPIError as exc:
                if exc.code != 700003 or exc.http_status != 400:
                    raise
                retry = state.get('timestamp_retry', 0) + 1
                if retry > 3:
                    raise Paused('MEXC 700003 повторился трижды; проверьте часы сервера и прокси') from exc
                state['timestamp_retry'] = retry
                save_state(journal, state)
                await wait_until(datetime.now(timezone.utc) + timedelta(seconds=3 * retry), stop_event)
                continue
            finally:
                if not cycle_id and not limit_skipped:
                    created = journal.db.execute('SELECT id FROM cycles WHERE rowid>? ORDER BY rowid',
                                                 (before,)).fetchall()
                    if len(created) > 1:
                        raise Paused('Несколько циклов созданы одновременно; нужна сверка')
                    if created:
                        state['active_cycle'] = created[0]['id']
                        save_state(journal, state)
                if state.get('active_cycle'):
                    _record_forward(journal, state, state['active_cycle'])
            cycle_id = state.get('active_cycle')
            if not cycle_id:
                raise Paused('Цикл не был сохранён; запуск следующего ордера остановлен')
            _record_forward(journal, state, cycle_id)
            state.pop('timestamp_retry', None)
            state.pop('last_error', None)
            cycle = journal.cycle(cycle_id)
            if cycle['status'] == 'completed':
                _finish_completed_cycle(journal, state, cycle_id, profile)
                if state['mode'] == 'eflp_volume' and telegram:
                    from sheets import Reporter
                    await Reporter(journal, telegram, None, keyboard=telegram_keyboard).flush()
                continue
            if _handle_forward_rejection(journal, state, cycle_id, profile):
                state.pop('current_profile', None)
                save_state(journal, state)
                if telegram and getattr(telegram, 'enabled', False):
                    from sheets import Reporter
                    await Reporter(journal, telegram, None, keyboard=telegram_keyboard).flush()
                continue
            if _switch_rejected_cash_reverse(journal, state, cycle_id, profile):
                if telegram and getattr(telegram, 'enabled', False):
                    route = journal.cycle(cycle_id)['spec'].get('cash_return_route')
                    await telegram.send(f'⚠️ П2 {p2_nickname(profile)}: обратный ордер явно отклонён MEXC. '
                                        + ('Возвращаю USDT через сеть, затем сменю профиль.' if route == 'network'
                                           else 'Возвращаю последний ордер через объявление П2, затем сменю профиль.'),
                                        reply_markup=telegram_keyboard() if telegram_keyboard else None)
                continue
            if (state['mode'] == 'cash_volume'
                    and journal.step(cycle_id, 'forward_complete')
                    and cycle['spec'].get('cash_route_selected')
                    and cycle['status'] not in {'abandoned', 'completed'}
                    and not journal.step(cycle_id, 'reverse_ad')):
                # The first leg completed and its return route was persisted.
                # Resume immediately with the correct AdsPower maker profile.
                continue
            raise Paused(f'Цикл {cycle_id} не завершён. Проверьте текущий ордер и продолжите его.')
    except OperatorStopped:
        state['status'] = 'paused'
        if not state.get('active_cycle'):
            state.pop('current_profile', None)
        save_state(journal, state)
        raise
    except Exception as exc:
        state['status'] = 'paused'
        if not state.get('active_cycle'):
            state.pop('current_profile', None)
        state['last_error'] = str(exc)[:500] if isinstance(exc, (ValueError, Paused)) else type(exc).__name__
        save_state(journal, state)
        raise
