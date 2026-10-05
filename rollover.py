"""Persistent scheduler for one-cycle-at-a-time P2P runs across named P2 profiles."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import json
import os
import random
import uuid

from config import Settings, p2_profile_name, p2_nickname
from cycle import OperatorStopped, Paused, fingerprint, run_command
from mexc_client import MexcAPIError, MexcChatUnavailable, MexcMutationUnknown, MexcP2PClient
from adspower import AdsPowerClickUnknown

KRS = timezone(timedelta(hours=7))
STATE_KEY = 'rollover_scheduler_v1'


def profiles_from_env(env=os.environ):
    names = [p2_profile_name(x.strip()) for x in env.get('ROLLOVER_PROFILES', 'default').split(',')]
    if not names or len(set(names)) != len(names):
        raise ValueError('ROLLOVER_PROFILES: укажите уникальные имена профилей через запятую')
    return names


def load_state(journal):
    row = journal.db.execute('SELECT value FROM meta WHERE key=?', (STATE_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def cycle_rowid(journal, cycle_id):
    row = journal.db.execute('SELECT rowid FROM cycles WHERE id=?', (cycle_id,)).fetchone()
    if not row:
        raise ValueError(f'Цикл {cycle_id} не найден в журнале')
    return row[0]


def save_state(journal, state):
    with journal.db:
        journal.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                           (STATE_KEY, json.dumps(state, ensure_ascii=False)))


def reset_with_pending_return(journal, state, cycle_id):
    """Stop local recovery but retain an audit snapshot of the unfinished return."""
    pending = state.get('pending_return') or {}
    if pending.get('cycle_id') != cycle_id or state.get('active_cycle') != cycle_id:
        raise ValueError('Состояние возврата изменилось; откройте сброс заново')
    if journal.cycle(cycle_id)['status'] in {'completed', 'abandoned'}:
        raise ValueError('Цикл уже завершён или сброшен')
    archived = dict(pending, archived_at=datetime.now(timezone.utc).isoformat())
    updated = dict(state)
    updated['archived_returns'] = [*state.get('archived_returns', []), archived]
    updated['active_cycle'] = None
    updated['pending_return'] = None
    updated['status'] = 'stopped'
    updated['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    updated.pop('last_error', None)
    with journal.db:
        journal.db.execute("UPDATE cycles SET status='abandoned' WHERE id=?", (cycle_id,))
        journal.db.execute("""INSERT INTO events
            (event_id,time,cycle_id,step,actor,status,order_no,amount,fiat,quantity,message)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, archived['archived_at'], cycle_id, 'cycle', 'operator', 'abandoned',
             '', '', '', str(pending.get('quantity', '')),
             f"Цикл сброшен локально; возврат USDT на этапе {pending.get('stage', 'unknown')} "
             'сохранён в архиве. Операций на MEXC не выполнялось.'))
        journal.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                           (STATE_KEY, json.dumps(updated, ensure_ascii=False)))
    state.clear()
    state.update(updated)


def amount_from_ad(ad):
    if (ad.get('side', 'SELL' if ad.get('tradeType') == 1 else None) != 'SELL'
            or ad.get('coinName') != 'USDT'):
        raise ValueError('Первое объявление П1 должно продавать USDT')
    if ad.get('advStatus') and ad['advStatus'] != 'OPEN':
        raise Paused('Объявление П1 сейчас не опубликовано')
    try:
        price = Decimal(str(ad['price']))
        max_fiat = Decimal(str(ad.get('maxSingleTransAmount', ad.get('maxTradeLimit'))))
        min_fiat = Decimal(str(ad.get('minSingleTransAmount', ad.get('minTradeLimit'))))
        available = Decimal(str(ad['availableQuantity']))
    except (KeyError, ValueError, ArithmeticError):
        raise ValueError('Не удалось прочитать цену, лимиты и остаток объявления') from None
    if not all(x.is_finite() for x in (price, max_fiat, min_fiat, available)) or price <= 0 or available <= 0:
        raise ValueError('Цена или остаток объявления недоступны')
    upper_usdt = min(max_fiat / price, available) - Decimal(110)
    if upper_usdt < 200:
        raise Paused(f'Сумма объявления: верхняя граница {upper_usdt:.2f} USDT; '
                     'требуется не менее 200 USDT. Снизился лимит или доступный остаток.')
    lower_usdt = max(upper_usdt - Decimal(100), min_fiat / price)
    low = (lower_usdt * price).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    high = (upper_usdt * price).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    if low <= 0 or low > high or high > max_fiat:
        raise Paused('Нет допустимого диапазона суммы в лимитах объявления П1')
    return low, high, price


def third_trade_anchor(journal, profile, when=None):
    """Third first-leg order of the most recent local trading day before a limit."""
    when = when or datetime.now(timezone.utc)
    rows = journal.db.execute("""SELECT e.time FROM events e JOIN cycles c ON c.id=e.cycle_id
        WHERE e.step='forward_create' AND e.status='done'
          AND COALESCE(json_extract(c.spec,'$.p2_profile'),'default')=?
          AND e.time<=? ORDER BY e.time DESC""", (profile, when.isoformat())).fetchall()
    dates = [datetime.fromisoformat(r[0]).astimezone(KRS) for r in rows]
    if not dates:
        return when, False
    days = sorted({t.date() for t in dates}, reverse=True)
    for day in days:
        same_day = sorted(t for t in dates if t.date() == day)
        if len(same_day) >= 3:
            return same_day[2].astimezone(timezone.utc), True
    latest = sorted(t for t in dates if t.date() == days[0])[-1]
    return latest.astimezone(timezone.utc), False


def cooldown_after_limit(journal, profile, when=None):
    when = when or datetime.now(timezone.utc)
    anchor, third = third_trade_anchor(journal, profile, when)
    until = anchor + timedelta(days=1)
    # A fresh 60085 after the recorded 24 hours cannot be treated as proof of available quota.
    return {'anchor': anchor.isoformat(), 'until': until.isoformat(),
            'third_trade': third, 'manual_block': until <= when}


def cooldown_after_ad_rejection(when=None):
    """85010 has no published reset time; use a local 24-hour retry delay."""
    when = when or datetime.now(timezone.utc)
    return {'anchor': when.isoformat(), 'until': (when + timedelta(days=1)).isoformat(),
            'manual_block': False, 'reason': '85010'}


def eligible(state, when=None):
    when = when or datetime.now(timezone.utc)
    names = state['profiles'] if state['mode'] == 'all' else [state['selected']]
    start = state.get('cursor', 0) % len(names)
    for offset in range(len(names)):
        name = names[(start + offset) % len(names)]
        limit = state['cooldowns'].get(name)
        if not limit or (not limit.get('manual_block') and datetime.fromisoformat(limit['until']) <= when):
            return name
    return None


def next_wait(state, when=None):
    when = when or datetime.now(timezone.utc)
    names = state['profiles'] if state['mode'] == 'all' else [state['selected']]
    future = [datetime.fromisoformat(state['cooldowns'][name]['until']) for name in names
              if name in state['cooldowns'] and not state['cooldowns'][name].get('manual_block')]
    return min(future) if future else None


def unblock(journal, profile):
    state = load_state(journal)
    if not state or profile not in state['profiles'] or profile not in state['cooldowns']:
        raise ValueError('У этого профиля нет сохранённого таймера')
    if state.get('pending_return') or state.get('active_cycle'):
        raise ValueError('Сначала завершите текущий цикл и возврат средств')
    state['cooldowns'][profile]['manual_block'] = False
    save_state(journal, state)


def clear_cooldown(journal, profile):
    """Clear only the selected profile's local timer; MEXC limits are unaffected."""
    state = load_state(journal)
    if not state or profile not in state.get('profiles', []) or profile not in state.get('cooldowns', {}):
        raise ValueError('У этого профиля нет сохранённого таймера')
    limit = state['cooldowns'][profile]
    if (state.get('mode') == 'volume' and limit.get('reason') == 'volume'
            and datetime.fromisoformat(limit['until']) > datetime.now(timezone.utc)):
        raise ValueError('В режиме «Объём» 24 часа от третьей сделки ещё не прошли; таймер не сброшен')
    del state['cooldowns'][profile]
    save_state(journal, state)


def finish_series(journal):
    state = load_state(journal)
    if not state or state['status'] in {'done', 'stopped'}:
        raise ValueError('Нет активной серии для завершения')
    if state.get('active_cycle') or state.get('pending_return'):
        raise ValueError('Сначала завершите текущий цикл и возврат средств')
    state['status'] = 'stopped'
    save_state(journal, state)


async def wait_until(when, stop_event):
    while datetime.now(timezone.utc) < when:
        if stop_event.is_set():
            raise OperatorStopped('Остановлено оператором; таймеры сохранены')
        seconds = min(30, (when - datetime.now(timezone.utc)).total_seconds())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            pass


async def choose_amount(profile, env=os.environ):
    settings = Settings.from_env('p1')
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                           settings.recv_window, proxy_url=settings.proxy_url)
    try:
        ad = await client.get_ad(env['MEXC_P1_SELL_ADV_NO'])
        low, high, _ = amount_from_ad(ad)
        fiat = ad.get('fiatUnit') or ad.get('currency')
        if not fiat:
            raise ValueError('Объявление не содержит фиатную валюту')
        cents = random.randint(int(low * 100), int(high * 100))
        return f'{Decimal(cents) / 100:.2f} {fiat}'
    finally:
        await client.close()


def begin(journal, mode, selected=None, env=os.environ):
    names = profiles_from_env(env)
    if mode not in {'all', 'single'} or (mode == 'single' and selected not in names):
        raise ValueError('Выберите доступный профиль П2')
    from config import p2_prefix
    active = names if mode == 'all' else [selected]
    members, keys = set(), set()
    for name in active:
        prefix = p2_prefix(name)
        for field in ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME', 'PAYMENT_ID'):
            if not env.get(f'{prefix}_{field}', '').strip():
                raise ValueError(f'Заполните {prefix}_{field} для профиля {name}')
        member, key = env[f'{prefix}_MEMBER_ID'], fingerprint(env[f'{prefix}_API_KEY'])
        if member in members or key in keys or member == env.get('MEXC_P1_MEMBER_ID'):
            raise ValueError('Профили П2 должны быть разными аккаунтами и отличаться от П1')
        members.add(member)
        keys.add(key)
    previous = load_state(journal)
    if previous and previous['status'] not in {'done', 'stopped'}:
        raise ValueError('Уже есть незавершённая серия; используйте «Продолжить»')
    journal.ensure_can_create()
    state = {'status': 'ready', 'mode': mode, 'selected': selected, 'profiles': names,
             'completed_count': 0, 'active_cycle': None,
             'cooldowns': previous.get('cooldowns', {}) if previous else {},
             'ad_rejection_streaks': previous.get('ad_rejection_streaks', {}) if previous else {},
             'ad_rejection_last_cycles': previous.get('ad_rejection_last_cycles', {}) if previous else {},
             'archived_returns': previous.get('archived_returns', []) if previous else [],
             'cursor': 0, 'pending_return': None,
             'last_cycle_rowid': journal.db.execute('SELECT COALESCE(MAX(rowid),0) FROM cycles').fetchone()[0]}
    save_state(journal, state)
    return state


async def run(journal, state, stop_event, telegram, telegram_keyboard=None):
    """Launches only confirmed complete cycles. A limit holds the affected profile."""
    if state.get('mode') in {'volume', 'unique', 'cash_volume', 'eflp_volume', 'eflp_unique'}:
        from trade_modes import run_mode
        return await run_mode(journal, state, stop_event, telegram, telegram_keyboard)
    from main import build_parser
    if 'last_cycle_rowid' not in state:
        known = set(state.pop('known_cycle_ids', []))
        state['last_cycle_rowid'] = max((row[0] for row in journal.db.execute('SELECT rowid,id FROM cycles')
                                          if row[1] in known), default=0)
        state['completed_count'] = len(state.pop('completed_ids', []))
        state.pop('target', None)
    state['status'] = 'running'
    state.pop('last_error', None)
    save_state(journal, state)
    try:
        while True:
            if stop_event.is_set():
                raise OperatorStopped('Остановлено оператором')
            if not state.get('active_cycle'):
                missed = journal.db.execute('SELECT id FROM cycles WHERE rowid>? ORDER BY rowid',
                                            (state['last_cycle_rowid'],)).fetchall()
                if len(missed) > 1:
                    raise Paused('Несколько неизвестных циклов в журнале; запуск следующей сделки остановлен')
                if missed:
                    recovered = journal.cycle(missed[0]['id'])
                    spec = recovered['spec']
                    if (spec.get('automatic') is not True
                            or spec.get('p2_profile', 'default') not in state['profiles']
                            or (state['mode'] == 'single' and spec.get('p2_profile', 'default') != state['selected'])
                            or spec.get('series', {}).get('count') != 1):
                        raise Paused('Найден посторонний цикл; продолжение серии остановлено')
                    state['active_cycle'] = recovered['id']
                    save_state(journal, state)
            if state.get('pending_return'):
                from return_funds import finish_return
                await finish_return(journal, state, stop_event)
                finish_limited_cycle(journal, state)
                continue
            cycle_id = state.get('active_cycle')
            if cycle_id:
                cycle = journal.cycle(cycle_id)
                profile = cycle['spec'].get('p2_profile', 'default')
                if state.get('pending_switch_notice'):
                    enqueue_switch_notice(journal, state, profile)
            else:
                profile = eligible(state)
                if profile is None:
                    deadline = next_wait(state)
                    if not deadline:
                        raise Paused('Все профили ограничены MEXC; доступного времени возобновления нет')
                    state['status'] = 'waiting'
                    save_state(journal, state)
                    await wait_until(deadline, stop_event)
                    state['status'] = 'running'
                    save_state(journal, state)
                    continue
                amount = await choose_amount(profile)
                args = build_parser().parse_args(['cycle', '--auto', '--p2-profile', profile,
                                                  '--amount', amount, '--count', '1'])
            if cycle_id:
                args = build_parser().parse_args(['cycle', '--resume', cycle_id])
            before_rowid = journal.db.execute('SELECT COALESCE(MAX(rowid),0) FROM cycles').fetchone()[0] if not cycle_id else 0
            from adspower import AdsPowerUnavailable
            from mexc_client import MexcReadUnavailable
            ads_unavailable = False
            mexc_read_unavailable = False
            chat_unavailable = False
            mutation_unknown = False
            mexc_read_error = None
            timestamp_rejected = False
            try:
                await run_command(args, stop_event=stop_event, use_lock=False,
                                  notify_prepare_errors=False, telegram_keyboard=telegram_keyboard)
            except AdsPowerUnavailable:
                ads_unavailable = True
            except MexcReadUnavailable as exc:
                mexc_read_unavailable = True
                mexc_read_error = str(exc)
            except MexcChatUnavailable:
                chat_unavailable = True
            except (MexcMutationUnknown, AdsPowerClickUnknown):
                mutation_unknown = True
            except MexcAPIError as exc:
                if exc.code != 700003 or exc.http_status != 400:
                    raise
                timestamp_rejected = True
            finally:
                if not cycle_id:
                    created = journal.db.execute('SELECT id FROM cycles WHERE rowid>? ORDER BY rowid',
                                                 (before_rowid,)).fetchall()
                    if len(created) > 1:
                        raise RuntimeError('Запущено несколько циклов вместо одного; остановлено')
                    if created:
                        state['active_cycle'] = created[0]['id']
                        save_state(journal, state)
                        if state.get('pending_switch_notice'):
                            enqueue_switch_notice(journal, state, profile)
            if timestamp_rejected:
                cid = state.get('active_cycle')
                latest = journal.db.execute('SELECT step FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT 1',
                                            (cid,)).fetchone() if cid else None
                step = latest[0] if latest else None
                saved = journal.step(cid, step) if step else None
                if not saved or saved['status'] != 'rejected' or saved['result'].get('rejected_code') != 700003:
                    raise Paused('MEXC 700003: нет подтверждённого отказа запроса; автоматический повтор остановлен')
                previous = state.get('timestamp_retry', {})
                count = previous.get('count', 0) + 1 if previous.get('cycle_id') == cid and previous.get('step') == step else 1
                if count > 3:
                    raise Paused(f'MEXC 700003 повторился для шага {step}; проверьте часы и прокси П2')
                state['timestamp_retry'] = {'cycle_id': cid, 'step': step, 'count': count}
                state['status'] = 'waiting'
                save_state(journal, state)
                await wait_until(datetime.now(timezone.utc) + timedelta(seconds=3 * count), stop_event)
                state['status'] = 'running'
                save_state(journal, state)
                continue
            state.pop('timestamp_retry', None)
            if ads_unavailable or mexc_read_unavailable or chat_unavailable or mutation_unknown:
                waiting_key = ('adspower_waiting' if ads_unavailable else
                               'mexc_read_waiting' if mexc_read_unavailable else
                               'chat_waiting' if chat_unavailable else 'mutation_waiting')
                service = ('AdsPower' if ads_unavailable else 'MEXC' if mexc_read_unavailable else
                           'Чат MEXC' if chat_unavailable else 'Операция MEXC')
                first_failure = not state.get(waiting_key)
                state[waiting_key] = True
                state['status'] = 'waiting'
                state['last_error'] = (f'Результат операции MEXC неизвестен; сверю сохранённый шаг через 30 секунд'
                                       if mutation_unknown else
                                       f'Связь с чатом MEXC прервалась; повторю сохранённое сообщение через 30 секунд'
                                       if chat_unavailable else
                                       f'{service} временно не отвечает на чтение: '
                                       f'{mexc_read_error}; повторная проверка через 30 секунд'
                                       if mexc_read_error else
                                       f'{service} временно не отвечает на чтение; повторная проверка через 30 секунд')
                save_state(journal, state)
                if first_failure and telegram and getattr(telegram, 'enabled', False):
                    if mutation_unknown:
                        message = ('⚠️ Ответ MEXC на действие не получен. Через 30 секунд бот сверит '
                                   'сохранённый шаг и продолжит только по подтверждённому состоянию.')
                    elif chat_unavailable:
                        message = ('⚠️ Связь с чатом MEXC прервалась. Через 30 секунд бот повторит '
                                   'сохранённое сообщение и продолжит тот же цикл. Сообщение может продублироваться.')
                    else:
                        detail = f' {mexc_read_error}.' if mexc_read_error else ''
                        message = (f'⚠️ {service} временно не отвечает на чтение.{detail} Серия ждёт 30 секунд '
                                   'и сверит сохранённый цикл снова; повторной операции без сверки не будет.')
                    await telegram.send(message,
                                        reply_markup=telegram_keyboard() if telegram_keyboard else None)
                await wait_until(datetime.now(timezone.utc) + timedelta(seconds=30), stop_event)
                state['status'] = 'running'
                save_state(journal, state)
                continue
            state.pop('last_error', None)
            state.pop('adspower_waiting', None)
            state.pop('mexc_read_waiting', None)
            state.pop('chat_waiting', None)
            state.pop('mutation_waiting', None)
            cycle_id = state.get('active_cycle')
            if not cycle_id:
                raise RuntimeError('MEXC-цикл не был сохранён; следующая сделка не запущена')
            cycle = journal.cycle(cycle_id)
            if cycle['status'] == 'completed':
                state['completed_count'] += 1
                state.setdefault('ad_rejection_streaks', {})[profile] = 0
                state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
                state['active_cycle'] = None
                # Keep using this P2 until MEXC reports a limit for that profile.
                save_state(journal, state)
                continue
            rejection = next(((key, s['result']['rejected_code'])
                              for key in ('forward_create', 'reverse_create')
                              if (s := journal.step(cycle_id, key)) and s['status'] == 'rejected'
                              and s['result'].get('rejected_code') in {60085, 85010}), None)
            if not rejection:
                raise Paused(f'Цикл {cycle_id} остановлен без подтверждённого лимита; требуется проверка ошибки')
            rejected_step, rejected_code = rejection
            if rejected_code == 85010:
                last_cycles = state.setdefault('ad_rejection_last_cycles', {})
                streaks = state.setdefault('ad_rejection_streaks', {})
                if last_cycles.get(profile) != cycle_id:
                    streaks[profile] = streaks.get(profile, 0) + 1
                    last_cycles[profile] = cycle_id
                    legacy = journal.create_rejection_details(cycle_id, rejected_step)
                    if legacy and legacy[0] == 85010:
                        rejected_at = datetime.fromisoformat(legacy[1])
                    else:
                        event = journal.db.execute(
                            "SELECT time FROM events WHERE cycle_id=? AND step=? "
                            "AND status='rejected' ORDER BY id DESC LIMIT 1",
                            (cycle_id, rejected_step)).fetchone()
                        rejected_at = datetime.fromisoformat(event['time']) if event else None
                    state['cooldowns'][profile] = cooldown_after_ad_rejection(rejected_at)
            else:
                state.setdefault('ad_rejection_streaks', {})[profile] = 0
                state['cooldowns'][profile] = cooldown_after_limit(journal, profile)
            state['pending_return'] = {'cycle_id': cycle_id, 'profile': profile} if journal.step(cycle_id, 'forward_complete') else None
            save_state(journal, state)
            if rejected_code == 85010 and streaks[profile] == 3 and not journal.step(cycle_id, 'ad_rejection_alert'):
                until = datetime.fromisoformat(state['cooldowns'][profile]['until']).astimezone(KRS)
                journal.transition(cycle_id, 'ad_rejection_alert', 'system', 'done',
                    f'🚨 MEXC 85010 третий раз подряд у П2 {p2_nickname(profile)}. '
                    f'Отказ на шаге {rejected_step}; профиль на таймере до {until:%d.%m.%Y %H:%M} Красноярск. '
                    'Проверьте причину отказа и возможность торговли по этому объявлению в MEXC.')
                if telegram and getattr(telegram, 'enabled', False):
                    from sheets import Reporter
                    await Reporter(journal, telegram, None, keyboard=telegram_keyboard).flush()
            if state['pending_return']:
                from return_funds import finish_return
                await finish_return(journal, state, stop_event)
            finish_limited_cycle(journal, state)
    except OperatorStopped:
        state['status'] = 'paused'
        save_state(journal, state)
        raise
    except Exception as exc:
        state['status'] = 'paused'
        state['last_error'] = str(exc)[:700] if isinstance(exc, (Paused, ValueError, MexcAPIError)) else type(exc).__name__
        save_state(journal, state)
        raise


def finish_limited_cycle(journal, state):
    cycle_id = state['active_cycle']
    cycle = journal.cycle(cycle_id)
    profile = cycle['spec'].get('p2_profile', 'default')
    returned = state.get('pending_return')
    if returned and returned.get('stage') != 'done':
        raise Paused('Возврат USDT ещё не завершён; другой профиль не запускается')
    if returned and not returned.get('credited'):
        raise Paused('Возврат отмечен завершённым без подтверждённой суммы депозита П1')
    rejection_code = next((step['result'].get('rejected_code')
                           for key in ('forward_create', 'reverse_create')
                           if (step := journal.step(cycle_id, key)) and step['status'] == 'rejected'
                           and step['result'].get('rejected_code') in {60085, 85010}), 60085)
    journal.transition(cycle_id, 'rollover', 'system', 'abandoned',
                       f'Цикл с отказом MEXC {rejection_code} закрыт после сверки возврата; обратной P2P-сделки нет',
                       cycle_status='abandoned')
    if returned:
        state['pending_switch_notice'] = {
            'cycle_id': cycle_id, 'profile': profile,
            'from_name': p2_nickname(profile, cycle['spec'].get('nicknames', {}).get('p2')),
            'credited': returned['credited'], 'reason_code': rejection_code}
    state['active_cycle'] = None
    state['pending_return'] = None
    state['last_cycle_rowid'] = cycle_rowid(journal, cycle_id)
    state['cursor'] = (state['profiles'].index(profile) + 1) % len(state['profiles'])
    save_state(journal, state)
    if returned and (state['mode'] == 'single' or len(state['profiles']) == 1):
        enqueue_switch_notice(journal, state, profile)


def enqueue_switch_notice(journal, state, next_profile):
    """Queue one durable Telegram message after a verified return and P2 selection."""
    notice = state.get('pending_switch_notice')
    if not notice:
        return
    cycle_id = notice['cycle_id']
    if not journal.step(cycle_id, 'rollover_switch_notice'):
        destination = (f"Следующий П2: {p2_nickname(next_profile)}" if next_profile != notice['profile']
                       else f"П2 остаётся {p2_nickname(next_profile)}; ждём окончания таймера")
        credited = format(Decimal(str(notice['credited'])).normalize(), 'f')
        reason = ('лимита' if notice.get('reason_code', 60085) == 60085
                  else 'отказа MEXC 85010')
        journal.transition(cycle_id, 'rollover_switch_notice', 'system', 'done',
                           f"✅ Возврат после {reason} завершён\n"
                           f"{notice['from_name']} → П1: {credited} USDT\n"
                           f"Объявление П1 пополнено.\n{destination}",
                           result={'next_profile': next_profile},
                           context={'quantity': notice['credited']})
    state.pop('pending_switch_notice', None)
    save_state(journal, state)


def status(state):
    if not state:
        return 'Серия профилей ещё не запускалась.'
    count = state.get('completed_count', len(state.get('completed_ids', [])))
    lines = [f"Серия: {state['status']}; {count} полных циклов (без лимита по количеству)",
             f"Режим: {state['mode']}", f"Профили П2: {', '.join(p2_nickname(name) for name in state['profiles'])}"]
    if state['active_cycle']:
        lines.append(f"Активный цикл: {state['active_cycle']}")
    if state.get('last_error'):
        lines.append('Последняя ошибка: ' + state['last_error'])
    if state['pending_return']:
        lines.append(f"Возврат USDT: {p2_nickname(state['pending_return']['profile'])}; этап: {state['pending_return'].get('stage', 'подготовка')}")
    for name, limit in state['cooldowns'].items():
        suffix = ' / требует проверки' if limit['manual_block'] else ''
        if limit.get('reason') == '85010':
            suffix += f" / 85010 подряд: {state.get('ad_rejection_streaks', {}).get(name, 0)}"
        lines.append(f"{p2_nickname(name)}: таймер до {datetime.fromisoformat(limit['until']).astimezone(KRS):%d.%m %H:%M} (Красноярск){suffix}")
    return '\n'.join(lines)
