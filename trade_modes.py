"""Persistent scheduling for the P2-maker volume and unique-counterparty modes.

The old on-chain recovery path remains in rollover.py for existing saved runs.
New runs never invoke that path: each completed first leg is returned by P2P.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import os
import random

from config import Settings, p2_nickname, p2_profile_name
from cycle import CashVolumeLimitReached, OperatorStopped, Paused, run_command
from mexc_client import MexcAPIError, MexcChatUnavailable, MexcMutationUnknown, MexcP2PClient, MexcReadUnavailable
from adspower import AdsPowerClickUnknown, AdsPowerTimeout, AdsPowerUnavailable
from phrases import phrases_for_mode
from rollover import (STATE_KEY, amount_from_ad, cycle_rowid, load_state, profiles_from_env,
                      save_state, wait_until)
from trade_profiles import (cash_unique_p1_profiles, eflp_p1_fiat, eflp_p1_profiles, eflp_p2_payment_id, mode_pay_method_id,
                            profile_from_env, profile_prefix,
                            settings_for_profile, split_eflp_p2_profiles, validate_unique_profiles)
from volume_policy import (TARGET_USDT, CEILING_USDT, CASH_TARGET_USDT, CASH_CEILING_USDT,
                           cooldown_until, record_purchase, rolling_cash_purchases,
                           rolling_cash_retry_at, window_for)


def configured_mode_profiles(mode: str, env=os.environ) -> tuple[str | None, list[str]]:
    """Read ordered profile keys for Telegram modes with fixed P2 selection."""
    fields = {'cash_volume': ('CASH_VOLUME_P1_PROFILE', 'CASH_VOLUME_P2_PROFILES'),
              'eflp_volume': (None, 'EFLP_VOLUME_P2_PROFILES'),
              'eflp_unique': (None, 'UNIQUE_P2_PROFILES'),
              'cash_unique': (None, 'UNIQUE_P2_PROFILES')}
    if mode not in fields:
        raise ValueError('Для этого режима профили выбираются в Telegram')
    p1_field, p2_field = fields[mode]
    raw = (env.get(p2_field) or (env.get('EFLP_UNIQUE_P2_PROFILES')
                                  if mode == 'eflp_unique' else '') or '').strip()
    if not raw or any(not item.strip() for item in raw.split(',')):
        raise ValueError(f'{p2_field}: укажите ключи П2 через запятую, например default,2,3')
    profiles = [p2_profile_name(item) for item in raw.split(',')]
    if mode in {'eflp_volume', 'eflp_unique'} and any(name.startswith('p1_') for name in profiles):
        raise ValueError(f'{p2_field}: ключи p1_2, p1_3 зарезервированы для отдельных П1')
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


def _eflp_target_reached(state: dict) -> bool:
    """Both requirements apply to the current P1, across both Eflp modes."""
    volume = sum((Decimal(value) for value in state.get('eflp_volume_by_profile', {}).values()),
                 Decimal('0'))
    unique = len(state.get('unique_done', []))
    if state['mode'] == 'eflp_volume':
        return volume >= Decimal('20000') and unique >= 20
    return unique >= 20


def _next_maker(state: dict) -> str | None:
    makers = state['p1_profiles']
    position = makers.index(state['p1_profile'])
    return makers[position + 1] if position + 1 < len(makers) else None


async def _final_order(state: dict, next_maker: str | None, *, env=os.environ) -> tuple[str, str]:
    """Buy the old SELL ad's residual except 10–12 USDT; check live limits."""
    maker = profile_from_env(state['p1_profile'], env)
    settings = settings_for_profile(maker.key)
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                          settings.recv_window, proxy_url=settings.proxy_url)
    try:
        ad = await _ad(client, maker.sell_adv_no, 'SELL')
        available = Decimal(str(ad['availableQuantity']))
        price = Decimal(str(ad['price']))
        minimum = Decimal(str(ad['minSingleTransAmount']))
        maximum = Decimal(str(ad['maxSingleTransAmount']))
        if not all(value.is_finite() and value > 0 for value in (available, price, minimum, maximum)):
            raise Paused('Некорректный остаток, цена или лимиты SELL-объявления П1')
        reserve_floor = max(1000, int((minimum / price * 100).to_integral_value(rounding=ROUND_UP)))
        if reserve_floor > 1200:
            raise Paused('Для сохранения доступности SELL-объявления его минимум превышает резерв 12 USDT')
        reserve = Decimal(random.randint(reserve_floor, 1200)) / 100
        quantity = (available - reserve).quantize(Decimal('0.0001'), rounding=ROUND_DOWN)
        if quantity <= 0:
            raise Paused('Остатка SELL-объявления П1 недостаточно для финального выкупа с резервом 10–12 USDT')
        fiat_amount = quantity * price
        if not minimum <= fiat_amount <= maximum:
            raise Paused('Весь остаток SELL-объявления минус 10–12 USDT не помещается в лимиты одного ордера')
        if next_maker:
            next_profile = profile_from_env(next_maker, env)
            next_settings = settings_for_profile(next_maker)
            next_client = MexcP2PClient(next_settings.api_key, next_settings.secret_key,
                next_settings.base_url, next_settings.recv_window, proxy_url=next_settings.proxy_url)
            try:
                buy = await _ad(next_client, (env.get(f'{next_profile.prefix}_BUY_ADV_NO') or '').strip(),
                                'BUY', 'RUB' if state['mode'] == 'cash_unique' else
                                eflp_p1_fiat(next_maker, env))
                buy_amount = quantity * Decimal(str(buy['price']))
                if not (Decimal(str(buy['minSingleTransAmount'])) <= buy_amount
                        <= Decimal(str(buy['maxSingleTransAmount']))
                        and quantity <= Decimal(str(buy['availableQuantity']))):
                    raise Paused('Финальный перевод не помещается в лимиты BUY-объявления следующего П1')
            finally:
                await next_client.close()
        return f'{fiat_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)} {ad["fiatUnit"]}', format(quantity, 'f')
    finally:
        await client.close()


def restore_eflp_progress(journal, state: dict, sales: list[dict], *, env=os.environ,
                          now: datetime | None = None) -> None:
    """Rebuild this week's Eflp progress from first sales and completed cycles."""
    from sheets import weekly_start

    current_week = weekly_start(now or datetime.now(timezone.utc))
    member_profiles = {member: name for name in state['profiles']
                       if (member := str(env.get(f'{profile_prefix(name)}_MEMBER_ID') or '').strip())}
    volumes = {name: Decimal('0') for name in state['profiles']}
    counted = []
    unique_done = []
    completed_cycles = []
    incomplete = set()
    for sale in sales:
        if (sale.get('scheduler_mode') not in {'eflp_volume', 'eflp_unique'}
                or sale.get('p1_profile') != state['p1_profile']):
            continue
        try:
            completed_at = datetime.fromisoformat(sale['completed_at'])
            quantity = Decimal(str(sale['quantity']))
            if completed_at.tzinfo is None or not quantity.is_finite() or quantity <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise ValueError('В журнале Eflp нет достоверной даты или суммы продажи; '
                             'новый ордер остановлен') from None
        if weekly_start(completed_at) != current_week:
            continue
        profile = member_profiles.get(str(sale.get('p2_member_id') or '').strip())
        if not profile:
            profile = sale.get('p2_profile')
            if profile not in state['profiles']:
                continue
            expected_member = str(env.get(f'{profile_prefix(profile)}_MEMBER_ID') or '').strip()
            if sale.get('p2_member_id') and sale['p2_member_id'] != expected_member:
                raise ValueError(f'Изменился MEMBER_ID П2 {profile}; объём Eflp требует сверки журнала')
        volumes[profile] += quantity
        counted.append(sale['cycle_id'])
        cycle = journal.cycle(sale['cycle_id'])
        returned = journal.step(sale['cycle_id'], 'reverse_complete')
        refilled = journal.step(sale['cycle_id'], 'reverse_replenish')
        buy_refilled = (journal.step(sale['cycle_id'], 'reverse_replenish_buy')
                        if cycle and cycle['spec'].get('buy_replenish') else None)
        complete = (cycle and cycle['status'] == 'completed'
                    and returned and returned['status'] == 'done'
                    and refilled and refilled['status'] == 'done'
                    and (not cycle['spec'].get('buy_replenish')
                         or buy_refilled and buy_refilled['status'] == 'done'))
        if complete:
            if sale['scheduler_mode'] == state['mode']:
                completed_cycles.append(sale['cycle_id'])
            if profile not in unique_done:
                unique_done.append(profile)
        else:
            incomplete.add(profile)
    state['eflp_volume_by_profile'] = {name: str(amount) for name, amount in volumes.items() if amount}
    state['eflp_volume_total'] = str(sum(volumes.values(), Decimal('0')))
    state['eflp_counted_cycles'] = counted
    state['eflp_completed_cycles'] = completed_cycles
    state['completed_count'] = len(completed_cycles)
    state['unique_done'] = unique_done
    state['eflp_done'] = [name for name in unique_done
                          if volumes[name] >= Decimal('20000') and name not in incomplete]
    state['eflp_week_start'] = current_week.isoformat()


def restore_cash_unique_progress(journal, state: dict, sales: list[dict], *, env=os.environ,
                                 now: datetime | None = None) -> None:
    """Count only confirmed cash-unique returns in the operational week."""
    from sheets import weekly_start

    week = weekly_start(now or datetime.now(timezone.utc))
    member_profiles = {str(env.get(f'{profile_prefix(name)}_MEMBER_ID') or '').strip(): name
                       for name in state['profiles']}
    done = []
    completed = []
    for sale in sales:
        if (sale.get('scheduler_mode') != 'cash_unique'
                or sale.get('p1_profile') != state['p1_profile']):
            continue
        try:
            completed_at = datetime.fromisoformat(sale['completed_at'])
            if completed_at.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise Paused('У продажи «Уникальные наличка» нет достоверной даты') from None
        if weekly_start(completed_at) != week:
            continue
        cycle = journal.cycle(sale['cycle_id'])
        if not cycle or cycle['status'] != 'completed':
            continue
        if any(not (step := journal.step(sale['cycle_id'], name)) or step['status'] != 'done'
               for name in ('forward_complete', 'reverse_complete', 'reverse_replenish',
                            'reverse_replenish_buy')):
            continue
        profile = member_profiles.get(str(sale.get('p2_member_id') or '').strip())
        if not profile:
            profile = sale.get('p2_profile')
            if profile not in state['profiles']:
                continue
        completed.append(sale['cycle_id'])
        if profile not in done:
            done.append(profile)
    state['unique_done'] = done
    state['cash_unique_completed_cycles'] = completed
    state['completed_count'] = len(completed)
    state['cash_unique_week_start'] = week.isoformat()


def begin_mode(journal, mode: str, *, p1_profile='p1', p2_profiles=None, env=os.environ):
    if mode not in {'volume', 'unique', 'cash_volume', 'cash_unique', 'eflp_volume', 'eflp_unique'}:
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
    elif mode in {'cash_volume', 'cash_unique'}:
        mode_pay_method_id('cash_volume', 'RUB', env)
        chosen = list(p2_profiles or [])
        if mode == 'cash_unique':
            if p1_profile not in cash_unique_p1_profiles(env):
                raise ValueError('П1 отсутствует в CASH_UNIQUE_P1_PROFILES')
            chosen, _ = split_eflp_p2_profiles(p1_profile, chosen, env)
            if len(chosen) < 25:
                raise ValueError('Для «Уникальные наличка» требуется минимум 25 разных П2 вне аккаунта П1')
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
            payment_id = env[f'{p2_prefix}_PAYMENT_ID'].strip()
            if not payment_id.isascii() or not payment_id.isdecimal() or int(payment_id) <= 0:
                raise ValueError(f'{p2_prefix}_PAYMENT_ID: укажите положительный ID реквизитов П2')
            member = env[f'{p2_prefix}_MEMBER_ID'].strip()
            key = env[f'{p2_prefix}_API_KEY'].strip()
            if member in members or key in keys:
                raise ValueError('П1 и П2 должны иметь разные MEMBER_ID и API-ключи')
            members.add(member)
            keys.add(key)
    elif mode in {'eflp_volume', 'eflp_unique'}:
        phrases_for_mode(mode, env)
        requested = list(p2_profiles or [])
        minimum = 1
        if p1_profile not in eflp_p1_profiles(env):
            raise ValueError('Выбранного П1 нет в EFLP_P1_PROFILES')
        if len(requested) != len(set(requested)) or any(name not in names for name in requested):
            raise ValueError('П2 Eflp должны быть разными и входить в ROLLOVER_PROFILES')
        chosen, _ = split_eflp_p2_profiles(p1_profile, requested, env)
        if len(chosen) < minimum:
            raise ValueError(f'После пропуска П2, совпадающих с П1, осталось {len(chosen)}; '
                             f'для этого режима нужно не менее {minimum} разных П2')
        p1 = profile_from_env(p1_profile, env)
        if not (env.get(f'{p1.prefix}_BUY_ADV_NO') or '').strip():
            raise ValueError('Для П1 нужны объявления продажи и покупки USDT')
        fiat = eflp_p1_fiat(p1_profile, env)
        mode_pay_method_id(mode, fiat, env)
        member_ids = {p1.member_id}
        api_keys = {env.get(f'{p1.prefix}_API_KEY', '').strip()}
        for name in chosen:
            prefix = profile_prefix(name)
            required = ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME')
            missing = [f'{prefix}_{field}' for field in required
                       if not (env.get(f'{prefix}_{field}') or '').strip()]
            if missing:
                raise ValueError('Для П2 Eflp не заполнено: ' + ', '.join(missing))
            eflp_p2_payment_id(name, fiat, env)
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
    completed_same_plan = (previous and previous['status'] == 'done'
                           and (previous['mode'] == mode == 'cash_unique'
                                or previous['mode'] in {'eflp_volume', 'eflp_unique'}
                                and mode in {'eflp_volume', 'eflp_unique'}))
    if completed_same_plan:
        from sheets import weekly_start
        current_week = weekly_start(datetime.now(timezone.utc)).isoformat()
        week_field = 'cash_unique_week_start' if mode == 'cash_unique' else 'eflp_week_start'
        if previous.get(week_field) == current_week:
            raise ValueError('Эта серия уже завершена за текущую неделю; новый запуск создаст повторный финальный ордер')
    journal.ensure_can_create()
    p1_profiles = (eflp_p1_profiles(env) if mode in {'eflp_volume', 'eflp_unique'} else
                   cash_unique_p1_profiles(env) if mode == 'cash_unique' else [p1_profile])
    if mode == 'cash_unique' and len(p1_profiles) != 2:
        raise ValueError('CASH_UNIQUE_P1_PROFILES: укажите ровно два П1 в порядке работы')
    if mode in {'cash_unique', 'eflp_volume', 'eflp_unique'}:
        if p1_profile != p1_profiles[0]:
            raise ValueError('Автоматическая серия должна начинаться с первого П1 в .env')
        for maker_key in p1_profiles:
            maker = profile_from_env(maker_key, env)
            if not (env.get(f'{maker.prefix}_BUY_ADV_NO') or '').strip():
                raise ValueError(f'{maker.prefix}_BUY_ADV_NO: нужен для перехода между П1')
            if mode in {'eflp_volume', 'eflp_unique'}:
                fiat = eflp_p1_fiat(maker_key, env)
                mode_pay_method_id(mode, fiat, env)
            eligible, _ = split_eflp_p2_profiles(maker_key, list(p2_profiles or chosen), env)
            if len(eligible) < (25 if mode == 'cash_unique' else 1):
                raise ValueError(f'Для П1 {maker_key} недостаточно отличающихся П2')
            if mode in {'eflp_volume', 'eflp_unique'}:
                for p2_key in eligible:
                    eflp_p2_payment_id(p2_key, fiat, env)
    state = {
        'status': 'ready', 'mode': mode, 'p1_profile': p1_profile,
        'p1_profiles': p1_profiles, 'all_profiles': list(p2_profiles or chosen),
        'cash_policy': 'rolling24_p1' if mode == 'cash_volume' else None,
        'profiles': chosen, 'selected': None, 'cursor': 0,
        'completed_count': 0, 'active_cycle': None, 'pending_return': None,
        'cooldowns': {key: value for key, value in previous.get('cooldowns', {}).items()
                      if mode != 'cash_volume' or value.get('reason') not in {'volume', 'volume_rolling', 'order_cap'}}
                     if previous else {},
        'volume_windows': dict(previous.get('volume_windows', {})) if previous else {},
        'return_dust_usdt': dict(previous.get('return_dust_usdt', {})) if previous else {},
        'unique_done': [], 'ad_rejection_streaks': dict(previous.get('ad_rejection_streaks', {})) if previous else {},
        'eflp_volume_by_profile': {}, 'eflp_counted_cycles': [],
        'eflp_completed_cycles': [], 'eflp_done': [],
        'last_cycle_rowid': journal.db.execute('SELECT COALESCE(MAX(rowid),0) FROM cycles').fetchone()[0],
    }
    if mode in {'eflp_volume', 'eflp_unique'}:
        restore_eflp_progress(journal, state, journal.sales(), env=env)
    elif mode == 'cash_unique':
        restore_cash_unique_progress(journal, state, journal.sales(), env=env)
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
        if (state['mode'] in {'unique', 'eflp_unique', 'cash_unique'} and name in state['unique_done']
                or state['mode'] == 'eflp_volume' and len(state['unique_done']) < 20
                and name in state['unique_done']):
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


def _eflp_volume_amount(sell, price, fiat, buy_min_usdt, buy_max_usdt):
    """Trade near the live ad cap without the legacy 200 USDT bot minimum."""
    try:
        maximum = Decimal(str(sell['maxSingleTransAmount']))
        minimum = Decimal(str(sell['minSingleTransAmount']))
        available = Decimal(str(sell['availableQuantity']))
    except (KeyError, ValueError, ArithmeticError):
        raise Paused('Не удалось прочитать лимиты объявления продажи П1') from None
    if (not all(value.is_finite() for value in (maximum, minimum, available))
            or maximum <= 0 or minimum < 0 or available <= 0):
        raise Paused('Некорректные лимиты объявления продажи П1')
    # Leave a cent and a small quantity buffer for exchange rounding.
    upper = min((maximum - Decimal('0.01')) / price,
                available - Decimal('0.0001'),
                buy_max_usdt - Decimal('0.0001'))
    lower = max(minimum / price, buy_min_usdt) + Decimal('0.0001')
    low_cents = int((lower * price * 100).to_integral_value(rounding='ROUND_CEILING'))
    high_cents = int((upper * price * 100).to_integral_value(rounding=ROUND_DOWN))
    if low_cents > high_cents or high_cents <= 0:
        raise Paused('У объявлений П1 нет пересечения лимитов для нового ордера')
    headroom = upper - lower
    if headroom >= 100:
        offset = Decimal(random.randint(100, min(150, int(headroom))))
        selected = upper - offset
        cents = int((selected * price * 100).to_integral_value(rounding=ROUND_DOWN))
    else:
        # A 100 USDT offset would make the order invalid; use the available cap.
        cents = high_cents
    cents = min(high_cents, max(low_cents, cents))
    return f'{Decimal(cents) / 100:.2f} {fiat}'


async def choose_mode_amount(mode, p1_key, p2_key, state, env=os.environ, journal=None,
                             reverse_p1_key: str | None = None):
    """Re-read the advertisements needed by this mode before every cycle."""
    p1 = profile_from_env(p1_key, env)
    eflp = mode in {'eflp_volume', 'eflp_unique'}
    p2 = None if eflp or mode in {'cash_volume', 'cash_unique'} else profile_from_env(p2_key, env)
    p1_settings = settings_for_profile(p1_key)
    settings = [p1_settings]
    if not eflp and mode not in {'cash_volume', 'cash_unique'}:
        settings.append(Settings.from_env('p2', p2_profile=p2_key))
    clients = [MexcP2PClient(s.api_key, s.secret_key, s.base_url, s.recv_window,
                            proxy_url=s.proxy_url) for s in settings]
    try:
        sell = await _ad(clients[0], p1.sell_adv_no, 'SELL')
        reverse = (await _ad(clients[1], p2.sell_adv_no, 'SELL', sell.get('fiatUnit'))
                   if p2 else None)
        buy = None
        if mode in {'cash_volume', 'cash_unique', 'eflp_volume', 'eflp_unique'}:
            buy_key = reverse_p1_key or p1_key
            buy_no = (env.get(f'{profile_prefix(buy_key)}_BUY_ADV_NO') or '').strip()
            if not buy_no:
                raise Paused('У П1 нет номера объявления покупки USDT')
            if buy_key == p1_key:
                buy_client = clients[0]
            else:
                buy_settings = settings_for_profile(buy_key)
                buy_client = MexcP2PClient(buy_settings.api_key, buy_settings.secret_key,
                    buy_settings.base_url, buy_settings.recv_window,
                    proxy_url=buy_settings.proxy_url)
                clients.append(buy_client)
            buy = await _ad(buy_client, buy_no, 'BUY', sell.get('fiatUnit'))
        fiat = sell.get('fiatUnit')
        if not fiat:
            raise Paused('У объявления П1 нет фиатной валюты')
        if eflp:
            expected_fiat = eflp_p1_fiat(p1_key, env)
            if fiat != expected_fiat:
                raise Paused(f'Объявление П1 имеет валюту {fiat}, а в '
                             f'{profile_prefix(p1_key)}_FIAT указано {expected_fiat}')
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
        if mode == 'eflp_volume':
            amount = _eflp_volume_amount(sell, p1_price, fiat, buy_min_usdt, buy_max_usdt)
        elif mode in {'volume', 'cash_volume', 'cash_unique'}:
            _, high_fiat, _ = amount_from_ad(sell)
            high_usdt = min(high_fiat / p1_price, reverse_max_usdt, buy_max_usdt)
            p1_min_usdt = Decimal(str(sell['minSingleTransAmount'])) / p1_price
            low_usdt = max(high_usdt - Decimal(100), p1_min_usdt,
                           reverse_min_usdt, buy_min_usdt)
            if high_usdt < 200:
                raise Paused(f'🚨 Доступный новый ордер меньше 200 USDT ({high_usdt:.2f}); проверьте объявления')
            amount = _fiat_amount(low_usdt, high_usdt, p1_price, fiat)
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
        if _eflp_sale_week(journal, cycle_id) != state.get('eflp_week_start'):
            return
        counted = state.setdefault('eflp_counted_cycles', [])
        if cycle_id not in counted:
            counted.append(cycle_id)
            profile = journal.cycle(cycle_id)['spec']['p2_profile']
            amounts = state.setdefault('eflp_volume_by_profile', {})
            amounts[profile] = str(Decimal(amounts.get(profile, '0'))
                                   + Decimal(saved['result']['quantity']))
            state['eflp_volume_total'] = str(sum((Decimal(value) for value in amounts.values()), Decimal('0')))
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


def _eflp_sale_week(journal, cycle_id: str) -> str:
    from sheets import weekly_start

    row = journal.db.execute('SELECT completed_at FROM sales WHERE cycle_id=?', (cycle_id,)).fetchone()
    if not row or not row['completed_at']:
        raise Paused('Для цикла Eflp нет подтверждённой первой продажи в журнале')
    return weekly_start(datetime.fromisoformat(row['completed_at'])).isoformat()


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


def _finish_completed_cycle(journal, state, cycle_id, profile, *, persist=True):
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
    eflp_current_week = (state['mode'] not in {'eflp_volume', 'eflp_unique'}
                         or _eflp_sale_week(journal, cycle_id) == state.get('eflp_week_start'))
    cash_unique_current_week = (state['mode'] != 'cash_unique'
                                or _eflp_sale_week(journal, cycle_id) == state.get('cash_unique_week_start'))
    _record_forward(journal, state, cycle_id)
    completed_cycles = state.setdefault('eflp_completed_cycles', []) if state['mode'] in {'eflp_volume', 'eflp_unique'} else None
    if eflp_current_week and cash_unique_current_week and (completed_cycles is None or cycle_id not in completed_cycles):
        state['completed_count'] += 1
        if completed_cycles is not None:
            completed_cycles.append(cycle_id)
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
    elif (state['mode'] == 'unique'
          or state['mode'] in {'eflp_unique', 'eflp_volume'} and eflp_current_week
          or state['mode'] == 'cash_unique' and cash_unique_current_week):
        if profile not in state['unique_done']:
            state['unique_done'].append(profile)
    state['active_cycle'] = None
    state.pop('current_profile', None)
    state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    if (state['mode'] in {'unique', 'cash_unique'}
            or state['mode'] in {'eflp_unique', 'eflp_volume'} and eflp_current_week
            or state['mode'] == 'cash_volume' and spec.get('reverse_maker') == 'p2'
            or cash_done):
        state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
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
    if persist:
        save_state(journal, state)


def _finish_terminal_cycle(journal, state, cycle_id: str, profile: str) -> None:
    pending = state.get('terminal_pending')
    if not pending or pending.get('from') != state['p1_profile'] or pending.get('p2') != profile:
        raise Paused('Финальный цикл не совпадает с сохранённым переходом П1')
    cycle = journal.cycle(cycle_id)
    if cycle['status'] != 'completed' or cycle['spec'].get('p1_profile') != pending['from']:
        raise Paused('Переход П1 остановлен: финальный цикл не подтверждён')
    if pending['kind'] == 'cash_regular':
        if cycle['spec'].get('reverse_p1_profile') != pending['to']:
            raise Paused('Обратный ордер 25-го цикла направлен не следующему П1')
        # Persist the completed cycle and the verified switch together. If the
        # process stops here, recovery must never open another 25th order.
        _finish_completed_cycle(journal, state, cycle_id, profile, persist=False)
    else:
        required = (('forward_complete',) if pending['kind'] == 'eflp_last' else
                    ('forward_complete', 'reverse_complete', 'reverse_replenish', 'reverse_replenish_buy'))
        if any(not (step := journal.step(cycle_id, name)) or step['status'] != 'done'
               for name in required):
            raise Paused('Финальная покупка или возврат новому П1 не подтверждены')
        if bool(cycle['spec'].get('forward_only')) != (pending['kind'] == 'eflp_last'):
            raise Paused('Маршрут финального ордера не совпадает с сохранённым переходом')
        if pending['kind'] != 'eflp_last' and cycle['spec'].get('reverse_p1_profile') != pending['to']:
            raise Paused('Финальный возврат направлен не следующему П1')
        state['active_cycle'] = None
        state.pop('current_profile', None)
        state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    pending['cycle_id'] = cycle_id
    pending['verified'] = True
    event_name = ('cash_unique_profile_done' if state['mode'] == 'cash_unique'
                  else 'eflp_profile_done')
    if not journal.step(cycle_id, event_name):
        label = profile_from_env(pending['from'], os.environ).nickname
        count = len(state.get('unique_done', []))
        volume = state.get('eflp_volume_total', '0')
        message = (f'✅ Уникальные наличка завершены: {label}, {count}/25 П2.'
                   if state['mode'] == 'cash_unique' else
                   f'✅ {"Объём" if state["mode"] == "eflp_volume" else "Уникальные"} Eflp '
                   f'для {label}: {count} уникальных П2, {volume} USDT.')
        journal.transition(cycle_id, event_name, 'system', 'done', message)
    save_state(journal, state)


async def _apply_terminal_switch(journal, state) -> None:
    """Close the completed maker before switching; repeated calls are safe."""
    pending = state.get('terminal_pending')
    if not pending or not pending.get('verified') or state.get('active_cycle'):
        raise Paused('Смена П1 возможна только после завершённого финального цикла')
    from adspower import AdsPower

    old = profile_from_env(pending['from'], os.environ)
    browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''), old.adspower_profile_id)
    await browser.ensure_stopped()
    target = pending.get('to')
    if target and pending.get('finish'):
        returned = profile_from_env(target, os.environ)
        await AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                       os.getenv('ADSPOWER_API_KEY', ''),
                       returned.adspower_profile_id).ensure_stopped()
    if target and not pending.get('finish'):
        eligible, _ = split_eflp_p2_profiles(target, state['all_profiles'], os.environ)
        if len(eligible) < (25 if state['mode'] == 'cash_unique' else 1):
            raise Paused('Для следующего П1 недостаточно П2 после исключения совпадающего аккаунта')
        state['p1_profile'] = target
        state['profiles'] = eligible
        state['cursor'] = 0
        state['unique_done'] = []
        state['completed_count'] = 0
        if state['mode'] == 'cash_unique':
            restore_cash_unique_progress(journal, state, journal.sales())
        else:
            restore_eflp_progress(journal, state, journal.sales())
        state['status'] = 'running'
    else:
        state['status'] = 'done'
    state['terminal_pending'] = None
    save_state(journal, state)


def _queue_eflp_mode_done(journal, state):
    """Persist one final notice so a failed Telegram send can be retried."""
    row = journal.db.execute('SELECT id FROM cycles WHERE rowid=?',
                             (state['last_cycle_rowid'],)).fetchone()
    if not row:
        raise Paused('Завершённый цикл Eflp не найден для итогового уведомления')
    cycle_id = row['id']
    cycle = journal.cycle(cycle_id)
    if (cycle['status'] != 'completed'
            or cycle['spec'].get('scheduler_mode') != state['mode']
            or cycle['spec'].get('p1_profile') != state['p1_profile']):
        raise Paused('Итоговое уведомление Eflp не соответствует сохранённому циклу')
    if journal.step(cycle_id, 'eflp_mode_done'):
        return
    p1_name = cycle['spec'].get('nicknames', {}).get('p1') or state['p1_profile']
    if state['mode'] == 'eflp_volume':
        total = sum((Decimal(value) for value in state['eflp_volume_by_profile'].values()), Decimal('0'))
        message = (f'✅ Объём Eflp завершён\nП1: {p1_name}\n'
                   f'П2 завершили: {len(state["eflp_done"])}/{len(state["profiles"])}\n'
                   f'Общий объём: {total} USDT')
    else:
        message = (f'✅ Уникальные Eflp завершены\nП1: {p1_name}\n'
                   f'Уникальных П2: {len(state["unique_done"])}')
    journal.transition(cycle_id, 'eflp_mode_done', 'system', 'done', message)


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


async def run_mode(journal, state, stop_event, telegram, telegram_keyboard=None, sheets=None):
    from main import build_parser

    async def refresh_eflp_progress():
        if sheets:
            from sheets import GoogleSheetsError
            try:
                sales = await sheets.read_eflp_sales(journal)
            except GoogleSheetsError as exc:
                raise Paused(f'Не удалось сверить прогресс Eflp с Google Таблицей: {exc}') from exc
            except Exception as exc:
                raise Paused(f'Не удалось прочитать прогресс Eflp из Google Таблицы '
                             f'({type(exc).__name__}); новый ордер не открыт') from exc
        else:
            sales = journal.sales()
        restore_eflp_progress(journal, state, sales)
        save_state(journal, state)

    async def refresh_cash_unique_progress():
        if sheets:
            from sheets import GoogleSheetsError
            try:
                sales = await sheets.read_eflp_sales(journal)
            except GoogleSheetsError as exc:
                raise Paused(f'Не удалось сверить уникальные наличные с Google Таблицей: {exc}') from exc
        else:
            sales = journal.sales()
        restore_cash_unique_progress(journal, state, sales)
        save_state(journal, state)

    state['status'] = 'running'
    save_state(journal, state)
    sheet_checked = False
    try:
        while True:
            if (state.get('terminal_pending') or {}).get('verified') and not state.get('active_cycle'):
                await _apply_terminal_switch(journal, state)
                sheet_checked = False
                if state['status'] == 'done':
                    return
                continue
            if state['mode'] in {'eflp_volume', 'eflp_unique'}:
                from sheets import weekly_start
                week_changed = (state.get('eflp_week_start')
                                != weekly_start(datetime.now(timezone.utc)).isoformat())
                if state.get('active_cycle'):
                    if week_changed:
                        restore_eflp_progress(journal, state, journal.sales())
                        save_state(journal, state)
                elif week_changed or sheets and not sheet_checked:
                    await refresh_eflp_progress()
                    sheet_checked = bool(sheets)
            elif state['mode'] == 'cash_unique':
                from sheets import weekly_start
                week_changed = (state.get('cash_unique_week_start')
                                != weekly_start(datetime.now(timezone.utc)).isoformat())
                if not state.get('active_cycle') and (week_changed or sheets and not sheet_checked):
                    await refresh_cash_unique_progress()
                    sheet_checked = bool(sheets)
            if stop_event.is_set():
                raise OperatorStopped('Остановлено оператором')
            cycle_id = state.get('active_cycle')
            if cycle_id:
                cycle = journal.cycle(cycle_id)
                profile = cycle['spec']['p2_profile']
                if state['mode'] in {'eflp_volume', 'eflp_unique'}:
                    _, same_as_p1 = split_eflp_p2_profiles(
                        state['p1_profile'], [profile], os.environ)
                    if same_as_p1:
                        raise Paused('Активный цикл Eflp использует П2, совпадающего с П1; '
                                     'проверьте ордера перед продолжением')
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
                    if state.get('terminal_pending'):
                        _finish_terminal_cycle(journal, state, cycle_id, profile)
                    else:
                        _finish_completed_cycle(journal, state, cycle_id, profile)
                        state['last_profile'] = profile
                        save_state(journal, state)
                    if (state['mode'] in {'eflp_volume', 'eflp_unique', 'cash_unique'}
                            and sheets and hasattr(sheets, 'send_weekly')):
                        from sheets import Reporter
                        from notifier import TelegramNotifier
                        await Reporter(journal, telegram or TelegramNotifier('', ''), sheets,
                                       keyboard=telegram_keyboard).flush(force_sheets=True)
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
                                and state['mode'] not in {'cash_volume', 'cash_unique', 'eflp_volume', 'eflp_unique'})
                            or spec.get('scheduler_mode') != state['mode']
                            or spec.get('p1_profile') != state['p1_profile']
                            or spec.get('p2_profile') not in state['profiles']
                            or spec.get('series', {}).get('count') != 1):
                        raise Paused('Найден посторонний цикл; запуск нового ордера остановлен')
                    state['active_cycle'] = missed[0]['id']
                    save_state(journal, state)
                    continue
                pending = state.get('terminal_pending')
                if pending and not pending.get('verified'):
                    if pending['from'] != state['p1_profile'] or pending['p2'] not in state['profiles']:
                        raise Paused('Сохранённый финальный переход не соответствует текущему П1 и П2')
                    command = ['cycle', '--auto', '--reverse-maker', 'p1',
                               '--scheduler-mode', state['mode'], '--p1-profile', pending['from'],
                               '--p2-profile', pending['p2'], '--amount', pending['amount'], '--count', '1']
                    if pending['kind'] == 'eflp_last':
                        command.append('--forward-only')
                    elif pending.get('to'):
                        command.extend(['--reverse-p1-profile', pending['to']])
                    if pending.get('quantity'):
                        command.extend(['--forward-quantity', pending['quantity']])
                    args = build_parser().parse_args(command)
                    before = journal.db.execute('SELECT COALESCE(MAX(rowid),0) FROM cycles').fetchone()[0]
                    try:
                        await run_command(args, stop_event=stop_event, use_lock=False,
                                          notify_prepare_errors=False, telegram_keyboard=telegram_keyboard)
                    except (MexcReadUnavailable, MexcChatUnavailable, MexcMutationUnknown,
                            AdsPowerClickUnknown, AdsPowerTimeout, AdsPowerUnavailable) as exc:
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
                            raise Paused('MEXC 700003 повторился трижды; проверьте время сервера и прокси') from exc
                        state['timestamp_retry'] = retry
                        save_state(journal, state)
                        await wait_until(datetime.now(timezone.utc) + timedelta(seconds=3 * retry), stop_event)
                        continue
                    finally:
                        created = journal.db.execute('SELECT id FROM cycles WHERE rowid>? ORDER BY rowid',
                                                     (before,)).fetchall()
                        if len(created) > 1:
                            raise Paused('Создано несколько финальных циклов; нужна сверка')
                        if created:
                            state['active_cycle'] = created[0]['id']
                            save_state(journal, state)
                    if not state.get('active_cycle'):
                        raise Paused('Финальный ордер не сохранён; продолжение остановлено')
                    state.pop('timestamp_retry', None)
                    state.pop('last_error', None)
                    continue
                if state['mode'] in {'eflp_volume', 'eflp_unique'}:
                    eligible, skipped = split_eflp_p2_profiles(
                        state['p1_profile'], state['profiles'], os.environ)
                    if skipped:
                        minimum = 1
                        if len(eligible) < minimum:
                            raise Paused(f'После пропуска П2, совпадающих с П1, осталось '
                                         f'{len(eligible)}; нужно не менее {minimum} П2')
                        cursor = state['cursor'] % len(state['profiles'])
                        ordered = state['profiles'][cursor:] + state['profiles'][:cursor]
                        next_name = next(name for name in ordered if name in eligible)
                        state['profiles'] = eligible
                        state['cursor'] = eligible.index(next_name)
                        state['eflp_done'] = [name for name in state.get('eflp_done', []) if name in eligible]
                        state['unique_done'] = [name for name in state.get('unique_done', []) if name in eligible]
                        save_state(journal, state)
                if state['mode'] == 'unique' and len(state['unique_done']) == len(state['profiles']):
                    state['status'] = 'done'
                    save_state(journal, state)
                    if telegram and getattr(telegram, 'enabled', False):
                        await telegram.send('✅ Уникальные: все выбранные П2 завершили по одному полному циклу.',
                                            reply_markup=telegram_keyboard() if telegram_keyboard else None)
                    return
                eflp_target_done = (state['mode'] in {'eflp_volume', 'eflp_unique'}
                                    and _eflp_target_reached(state))
                cash_unique_done = state['mode'] == 'cash_unique' and len(state['unique_done']) >= 25
                if eflp_target_done or cash_unique_done:
                    target = (_next_maker(state) if eflp_target_done else state['p1_profiles'][0])
                    candidates, _ = split_eflp_p2_profiles(
                        target or state['p1_profile'], state['profiles'], os.environ)
                    if not candidates:
                        raise Paused('Нет П2, отличного от обоих П1, для финального ордера')
                    profile = state.get('last_profile') if state.get('last_profile') in candidates else candidates[-1]
                    amount, quantity = await _final_order(state, target)
                    state['terminal_pending'] = {'kind': ('cash_final' if cash_unique_done else
                                                          'eflp_cross' if target else 'eflp_last'),
                                                 'from': state['p1_profile'], 'to': target,
                                                 'p2': profile, 'amount': amount, 'quantity': quantity,
                                                 'finish': cash_unique_done or target is None}
                    save_state(journal, state)
                    continue
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
                next_cash_maker = (_next_maker(state)
                                   if state['mode'] == 'cash_unique' and len(state['unique_done']) == 24
                                   else None)
                amount = await choose_mode_amount(state['mode'], state['p1_profile'], profile, state,
                                                  journal=journal, reverse_p1_key=next_cash_maker)
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
                if next_cash_maker:
                    target = next_cash_maker
                    candidates, _ = split_eflp_p2_profiles(target, [profile], os.environ)
                    if not candidates:
                        raise Paused('Последний П2 первого П1 совпадает со следующим П1')
                    state['terminal_pending'] = {'kind': 'cash_regular', 'from': state['p1_profile'],
                                                 'to': target, 'p2': profile, 'amount': amount,
                                                 'finish': False}
                save_state(journal, state)
                command = ['cycle', '--auto', '--reverse-maker',
                    'p1' if state['mode'] in {'cash_volume', 'cash_unique', 'eflp_volume', 'eflp_unique'} else 'p2',
                    '--scheduler-mode', state['mode'],
                    '--p1-profile', state['p1_profile'], '--p2-profile', profile,
                    '--amount', amount, '--count', '1']
                if (state.get('terminal_pending') or {}).get('kind') == 'cash_regular':
                    command.extend(['--reverse-p1-profile', state['terminal_pending']['to']])
                args = build_parser().parse_args(command)
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
            except (MexcReadUnavailable, MexcChatUnavailable, MexcMutationUnknown,
                    AdsPowerClickUnknown, AdsPowerTimeout, AdsPowerUnavailable) as exc:
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
                if state.get('terminal_pending'):
                    _finish_terminal_cycle(journal, state, cycle_id, profile)
                else:
                    _finish_completed_cycle(journal, state, cycle_id, profile)
                    state['last_profile'] = profile
                    save_state(journal, state)
                if (state['mode'] in {'eflp_volume', 'eflp_unique', 'cash_unique'}
                        and sheets and hasattr(sheets, 'send_weekly')):
                    from sheets import Reporter
                    from notifier import TelegramNotifier
                    await Reporter(journal, telegram or TelegramNotifier('', ''), sheets,
                                   keyboard=telegram_keyboard).flush(force_sheets=True)
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
