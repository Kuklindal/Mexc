"""Single-process Telegram control of the existing resumable cycle runner."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import logging
import os
from pathlib import Path
import secrets
import signal

from config import PROJECT_DIR, Settings, select_p2_profile, p2_nickname
from cycle import OperatorStopped, Paused, auto_plan, run_command, steps_for_spec
from journal import Journal, process_lock
from notifier import TelegramNotifier, TelegramRequestError
from sheets import GoogleSheets, Reporter

KRASNOYARSK = timezone(timedelta(hours=7))


def daily_stats(journal: Journal, moment: datetime | None = None) -> str:
    today = (moment or datetime.now(timezone.utc)).astimezone(KRASNOYARSK).date()
    rows = journal.db.execute("""SELECT * FROM events WHERE id IN (
        SELECT MIN(id) FROM events WHERE
        (step IN ('forward_complete','reverse_complete') AND status='done')
        OR (step='cycle' AND status='completed') GROUP BY cycle_id,step)""").fetchall()
    totals = {leg: {'count': 0, 'usdt': Decimal(0), 'fiat': {}} for leg in ('forward', 'reverse')}
    completed = 0
    for row in rows:
        if datetime.fromisoformat(row['time']).astimezone(KRASNOYARSK).date() != today:
            continue
        if row['step'] == 'cycle':
            completed += 1
            continue
        total = totals[row['step'].split('_')[0]]
        total['count'] += 1
        total['usdt'] += Decimal(row['quantity'] or '0')
        fiat = row['fiat'] or 'не указана'
        total['fiat'][fiat] = total['fiat'].get(fiat, Decimal(0)) + Decimal(row['amount'] or '0')
    lines = [f"📊 Сегодня, {today:%d.%m.%Y} (Красноярск, UTC+7)", f"Полностью завершено циклов: {completed}"]
    for leg, label in (('forward', 'Первые продажи П1 → П2'), ('reverse', 'Обратные сделки П2 → П1')):
        total = totals[leg]
        fiat = ', '.join(f'{amount:f} {currency}' for currency, amount in sorted(total['fiat'].items())) or '0'
        lines.append(f"{label}: {total['count']}\n{total['usdt']:f} USDT; суммы: {fiat}")
    lines.append('По локальному журналу завершений; незавершённые сделки не включены.')
    return '\n'.join(lines)


class TelegramControl:
    def __init__(self, journal: Journal, telegram: TelegramNotifier, owner_id: int, p2_profile: str,
                 sheets: GoogleSheets | None = None):
        self.journal, self.telegram = journal, telegram
        self.sheets = sheets
        self.owner_id, self.p2_profile = owner_id, p2_profile
        self.stop_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.reset_target: str | None = None
        self.timer_target: str | None = None
        self.chat_recovery: dict | None = None
        self.menu: str | None = None
        self.unique_p1: str | None = None
        self.unique_p2: set[str] = set()
        self.cash_p1: str | None = None
        self.cash_p2: set[str] = set()
        self.selected_mode: str | None = None
        self.nonce = secrets.token_hex(6)
        self.offset_key = 'telegram_offset_' + hashlib.sha256(telegram.bot_token.encode()).hexdigest()[:16]
        row = journal.db.execute('SELECT value FROM meta WHERE key=?', (self.offset_key,)).fetchone()
        self.offset = int(row[0]) if row else 0
        self.logger = logging.getLogger('mexc_p2p.control')

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def request_shutdown(self):
        """Remember a running series only for a service shutdown, not Telegram Stop."""
        if (os.getenv('AUTO_RESUME_ON_BOOT', 'false').lower() == 'true'
                and self.running and not self.stop_event.is_set()):
            from rollover import load_state, save_state
            state = load_state(self.journal)
            if state and state.get('status') in {'running', 'waiting'}:
                state['resume_on_boot'] = True
                save_state(self.journal, state)
        self.stop_event.set()
        self.shutdown_event.set()

    def resume_after_restart(self) -> bool:
        """Resume only a series interrupted while running, never an operator pause."""
        if os.getenv('AUTO_RESUME_ON_BOOT', 'false').lower() != 'true':
            return False
        from rollover import load_state, save_state
        state = load_state(self.journal)
        if not state or not (state.get('resume_on_boot') or state.get('status') in {'running', 'waiting'}):
            return False
        state.pop('resume_on_boot', None)
        save_state(self.journal, state)
        self.task = asyncio.create_task(self.work_rollover(state))
        return True

    def keyboard(self, *, force_idle=False):
        from rollover import load_state, profiles_from_env
        scheduler = load_state(self.journal)
        pending = self.unfinished()
        series_active = scheduler and scheduler.get('status') not in {'done', 'stopped'}
        rows = []
        if self.running and not force_idle:
            rows.append((('⏹ Остановить', 'stop'),))
        elif pending or series_active:
            if pending and self.pending_chat(pending):
                rows.append((('🔎 Сверить сообщение', 'chat_review'),))
            else:
                rows.append((('▶️ Продолжить', 'resume'),))
            if pending and self.reset_target != pending['id']:
                rows.append((('🗑 Сбросить цикл', 'reset'),))
            if series_active and not scheduler.get('active_cycle') and not scheduler.get('pending_return'):
                rows.append((('🔀 Сменить П2', 'switch'), ('🧹 Завершить серию', 'finish')))
        else:
            rows.append((('💵 Объём наличка', 'cash_volume'),))
            rows.append((('📈 Объём Eflp', 'eflp_volume'), ('👥 Уникальные Eflp', 'eflp_unique')))

        if (not self.running or force_idle) and not pending and not series_active:
            rows.append((('➕ Добавить П2', 'add_profile'),))
        if self.reset_target and pending and self.reset_target == pending['id']:
            rows.append((('⚠️ Подтвердить сброс', 'reset_confirm:' + self.reset_target),
                         ('Отмена', 'reset_cancel')))
        if (not self.running or force_idle) and self.chat_recovery and self.pending_chat(pending) == self.chat_recovery['target']:
            if self.chat_recovery.get('choice'):
                rows.append((('✅ Подтвердить сообщение', 'chat_confirm'), ('Отмена', 'chat_cancel')))
            else:
                rows.append((('✅ Уже есть в чате', 'chat_already'), ('↻ Отправить из бота', 'chat_retry')))
                rows.append((('Отмена', 'chat_cancel'),))
        if (not self.running or force_idle) and self.menu in {'start_profiles', 'switch_profiles'}:
            names = profiles_from_env()
            if self.menu == 'start_profiles' and not pending and not series_active:
                rows.extend(tuple((p2_nickname(name), 'single:' + name) for name in names[i:i + 3])
                            for i in range(0, len(names), 3))
            elif self.menu == 'switch_profiles' and series_active and not scheduler.get('active_cycle') and not scheduler.get('pending_return'):
                available = [name for name in names if name in scheduler.get('profiles', [])
                             and name not in scheduler.get('unique_done', [])]
                rows.extend(tuple(('⇄ ' + p2_nickname(name), 'select:' + name) for name in available[i:i + 3])
                            for i in range(0, len(available), 3))
            rows.append((('↩️ Назад', 'back'),))
        if (not self.running or force_idle) and not pending and not series_active:
            if self.menu == 'cash_p1':
                from trade_profiles import eflp_p1_profiles, ready_p1_profiles, profile_prefix
                from trade_modes import configured_mode_profiles
                try:
                    fixed_p2 = (set(configured_mode_profiles(self.selected_mode)[1])
                                if self.selected_mode in {'eflp_volume', 'eflp_unique'} else set())
                except ValueError:
                    fixed_p2 = set()
                p1_keys = (eflp_p1_profiles(os.environ)
                           if self.selected_mode in {'eflp_volume', 'eflp_unique'}
                           else ['p1', *profiles_from_env()])
                choices = [item for item in ready_p1_profiles(p1_keys, os.environ, include_main=False)
                           if item.key not in fixed_p2
                           and os.getenv(f'{profile_prefix(item.key)}_BUY_ADV_NO', '').strip()]
                rows.extend(tuple((item.nickname, 'cash_p1:' + item.key) for item in choices[i:i + 2])
                            for i in range(0, len(choices), 2))
                rows.append((('↩️ Назад', 'back'),))
            elif self.menu == 'cash_p2':
                from trade_profiles import profile_from_env
                choices = []
                for name in profiles_from_env():
                    if name == self.cash_p1:
                        continue
                    try:
                        if self.selected_mode == 'cash_volume':
                            profile = profile_from_env(name, os.environ)
                            nickname = profile.nickname
                        else:
                            from trade_profiles import profile_prefix
                            prefix = profile_prefix(name)
                            if not all(os.getenv(f'{prefix}_{field}', '').strip() for field in
                                       ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME', 'PAYMENT_ID')):
                                continue
                            nickname = os.getenv(f'{prefix}_NICKNAME', name)
                    except ValueError:
                        continue
                    choices.append((('✅ ' if name in self.cash_p2 else '□ ') + nickname,
                                    'cash_p2:' + name))
                rows.extend(tuple(choices[i:i + 2]) for i in range(0, len(choices), 2))
                minimum = 20 if self.selected_mode == 'eflp_unique' else 1
                if len(self.cash_p2) >= minimum:
                    rows.append(((f'▶️ Запустить: {len(self.cash_p2)} П2', 'cash_start'),))
                rows.append((('↩️ Назад', 'back'),))
            if self.menu == 'unique_p1':
                from trade_profiles import ready_p1_profiles
                choices = ready_p1_profiles(profiles_from_env(), os.environ)
                rows.extend(tuple((item.nickname, 'unique_p1:' + item.key) for item in choices[i:i + 2])
                            for i in range(0, len(choices), 2))
                rows.append((('↩️ Назад', 'back'),))
            elif self.menu == 'unique_p2':
                from trade_profiles import profile_from_env
                choices = []
                for name in profiles_from_env():
                    if name == self.unique_p1:
                        continue
                    try:
                        profile = profile_from_env(name, os.environ)
                    except ValueError:
                        continue
                    choices.append((('✅ ' if name in self.unique_p2 else '□ ') + profile.nickname,
                                    'unique_p2:' + name))
                rows.extend(tuple(choices[i:i + 2]) for i in range(0, len(choices), 2))
                if len(self.unique_p2) >= 20:
                    rows.append(((f'▶️ Запустить {len(self.unique_p2)} П2', 'unique_start'),))
                rows.append((('↩️ Назад', 'back'),))
        if scheduler and (not self.running or force_idle):
            now = datetime.now(timezone.utc)
            timed = [name for name, value in scheduler.get('cooldowns', {}).items()
                     if (value.get('manual_block') or datetime.fromisoformat(value['until']) > now)
                     and value.get('reason') != 'volume']
            if timed and self.menu != 'timer_confirm':
                rows.append((('⏱ Таймеры П2', 'timers'),))
            if self.menu == 'timer_profiles':
                rows.extend(tuple((p2_nickname(name), 'timer:' + name) for name in timed[i:i + 2])
                            for i in range(0, len(timed), 2))
                rows.append((('↩️ Назад', 'back'),))
            elif self.menu == 'timer_confirm' and self.timer_target:
                rows.append((('⚠️ Сбросить таймер', 'timer_confirm:' + self.timer_target),
                             ('Отмена', 'timer_cancel')))
        rows.append((('📍 Статус', 'status'), ('📊 Сегодня', 'stats')))
        return {'inline_keyboard': [[{'text': label, 'callback_data': self.nonce + ':' + action}
                                     for label, action in row] for row in rows]}

    async def reply(self, text, *, force_idle=False):
        await self.telegram.send(text, reply_markup=self.keyboard(force_idle=force_idle))

    def current(self):
        rows = self.journal.cycles()
        pending = next((row for row in rows if row['status'] not in {'completed', 'abandoned'}), None)
        return self.journal.cycle((pending or rows[0])['id']) if rows else None

    def unfinished(self):
        row = next((row for row in self.journal.cycles()
                    if row['status'] not in {'completed', 'abandoned'}), None)
        return self.journal.cycle(row['id']) if row else None

    def pending_chat(self, cycle: dict | None) -> dict | None:
        if not cycle or cycle['status'] in {'completed', 'abandoned'}:
            return None
        for step in steps_for_spec(cycle['spec']):
            saved = self.journal.step(cycle['id'], step.key)
            if saved and saved['status'] in {'unknown', 'in_flight'}:
                if step.key not in {'forward_message', 'forward_reply', 'reverse_message', 'reverse_reply'}:
                    return None
                order = self.journal.step(cycle['id'], step.key.split('_')[0] + '_create')
                order_no = (order or {}).get('result', {}).get('order_no')
                if not order_no or (saved['result'].get('order_no') and saved['result']['order_no'] != order_no):
                    return None
                return {'cycle_id': cycle['id'], 'step': step.key, 'actor': step.actor,
                        'order_no': order_no, 'text': saved['result'].get('text')}
        return None

    async def recover_chat(self, action: str):
        if self.running:
            return await self.reply('Сначала останови выполнение цикла.')
        cycle = self.unfinished()
        target = self.pending_chat(cycle)
        if not target:
            self.chat_recovery = None
            return await self.reply('Шага сообщения с неизвестным результатом нет. Проверь статус цикла.')
        if action == 'chat_cancel':
            self.chat_recovery = None
            return await self.reply('Сверка отменена. Журнал не изменён.')
        if action == 'chat_review':
            self.chat_recovery = {'target': target, 'choice': None}
            return await self.reply(f"Цикл {target['cycle_id']}, {target['step']}, {target['actor']}, "
                f"ордер {target['order_no']}. Отправка могла пройти, хотя соединение оборвалось. "
                'Проверь чат этого ордера на MEXC. Если сообщение уже есть, выбери «Уже есть в чате». '
                'Если его нет, выбери «Отправить из бота».')
        state = self.chat_recovery
        if not state or state['target'] != target:
            self.chat_recovery = None
            return await self.reply('Подтверждение устарело. Открой сверку сообщения заново.')
        if action in {'chat_already', 'chat_retry'}:
            from phrases import PHRASES
            state['choice'] = action
            state['text'] = target['text'] or (secrets.choice(PHRASES[target['step']])
                                               if action == 'chat_retry' else '')
            note = ('Никакого сообщения бот не отправит; шаг будет зачтён по твоей сверке.'
                    if action == 'chat_already' else 'Бот отправит ровно этот текст один раз. Перед подтверждением убедись, что его ещё нет в чате.')
            return await self.reply(f"Ордер {target['order_no']}, отправитель {target['actor']}.\n"
                f"Текст: {state['text'] or 'не сохранился в старой попытке'}\n{note}\nПодтверди кнопкой ниже или отмени.")
        if action != 'chat_confirm' or state.get('choice') not in {'chat_already', 'chat_retry'}:
            return await self.reply('Выбери способ сверки сообщения.')
        text_value = state['text']
        if state['choice'] == 'chat_retry':
            from config import Settings
            from cycle import fingerprint
            from mexc_client import MexcP2PClient, counterparty_identity
            profile = cycle['spec'].get('p2_profile', 'default')
            actor = target['actor']
            selected = (profile if actor == 'p2' else cycle['spec'].get('p1_profile', 'p1'))
            from trade_profiles import settings_for_profile
            settings = settings_for_profile(selected)
            if fingerprint(settings.api_key) != cycle['spec'].get('profiles', {}).get(actor):
                return await self.reply('API-ключ профиля изменился. Сообщение не отправлено.')
            client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                                  settings.recv_window, proxy_url=settings.proxy_url)
            try:
                detail = await client.get_order_detail(target['order_no'])
                expected_actor = 'p1' if actor == 'p2' else 'p2'
                member, nickname = counterparty_identity(detail)
                expected_id = cycle['spec'].get('members', {}).get(expected_actor)
                expected_nick = cycle['spec'].get('nicknames', {}).get(expected_actor)
                ad_step = self.journal.step(cycle['id'], target['step'].split('_')[0] + '_ad')
                expected_ad = (ad_step or {}).get('result', {}).get('adv_no')
                if (detail.get('advOrderNo') != target['order_no'] or not expected_id or member != expected_id
                        or (expected_actor == 'p2' and not expected_nick)
                        or (expected_nick and nickname != expected_nick)
                        or not expected_ad or detail.get('advNo') != expected_ad
                        or detail.get('coinName') != 'USDT'
                        or detail.get('fiatUnit') != cycle['spec'].get('fiat')):
                    return await self.reply('Ордер или контрагент не совпал с сохранённым циклом. Сообщение не отправлено.')
                self.journal.transition(cycle['id'], target['step'], actor, 'in_flight',
                    'Повторная отправка сообщения из Telegram', result={'order_no': target['order_no'], 'text': text_value},
                    context={'order_no': target['order_no']})
                try:
                    await client.send_chat_text(target['order_no'], text_value)
                except Exception as exc:
                    self.journal.transition(cycle['id'], target['step'], actor, 'unknown',
                        'Результат повторной отправки требует сверки', result={'order_no': target['order_no'], 'text': text_value},
                        context={'order_no': target['order_no']})
                    self.chat_recovery = None
                    return await self.reply(f'Отправка не подтверждена ({type(exc).__name__}). Проверь чат; автоматического повтора нет.')
            finally:
                await client.close()
        self.journal.transition(cycle['id'], target['step'], target['actor'], 'done',
            'Сообщение сверено оператором через Telegram',
            result={'order_no': target['order_no'], 'text': text_value, 'chat_read': False},
            context={'order_no': target['order_no']})
        self.chat_recovery = None
        return await self.reply(f"Шаг {target['step']} ордера {target['order_no']} зачтён. Нажми «Продолжить».")

    def status(self):
        from rollover import load_state

        scheduler = load_state(self.journal)
        cycle = self.unfinished()
        if self.running and self.stop_event.is_set():
            bot_state = 'Останавливается'
        elif self.running and scheduler and scheduler.get('mexc_read_waiting'):
            bot_state = 'Ждёт ответа MEXC; повтор чтения каждые 30 секунд'
        elif (self.running and scheduler and scheduler.get('status') == 'waiting'
              and scheduler.get('last_error') and not cycle):
            bot_state = 'Повторяет подключение'
        elif self.running:
            bot_state = 'Работает'
        elif cycle or (scheduler and scheduler.get('status') == 'paused'):
            bot_state = 'На паузе'
        else:
            bot_state = 'Не запущен'
        lines = [f'📍 Бот: {bot_state}']
        if scheduler:
            mode_label = {'all': 'все профили (старый)', 'single': 'один профиль (старый)',
                          'volume': 'Объём (старый)', 'unique': 'Уникальные (старый)',
                          'cash_volume': 'Объём наличка', 'eflp_volume': 'Объём Eflp',
                          'eflp_unique': 'Уникальные Eflp'}
            lines.append('Режим: ' + mode_label.get(scheduler.get('mode'), 'неизвестный'))
            lines.append(f"Завершено циклов: {scheduler.get('completed_count', 0)}")
            if scheduler.get('mode') in {'unique', 'cash_volume', 'eflp_volume', 'eflp_unique'}:
                from trade_profiles import profile_from_env
                p1_key = scheduler.get('p1_profile', 'p1')
                try:
                    p1_name = profile_from_env(p1_key, os.environ).nickname
                except ValueError:
                    p1_name = p1_key
                if scheduler.get('mode') == 'unique':
                    lines.append(f"П1: {p1_name}; П2 завершили: {len(scheduler.get('unique_done', []))}/{len(scheduler['profiles'])}")
                else:
                    lines.append(f"П1: {p1_name}; П2 по очереди: " + ', '.join(
                        p2_nickname(name) for name in scheduler['profiles']))
                if scheduler.get('mode') in {'eflp_volume', 'eflp_unique'}:
                    if scheduler.get('mode') == 'eflp_volume':
                        current_profile = scheduler.get('current_profile')
                        if current_profile:
                            lines.append('Объём этого П2: '
                                         + scheduler.get('eflp_volume_by_profile', {}).get(current_profile, '0')
                                         + ' / 20000 USDT')
                        lines.append(f'П2 выполнили план: {len(scheduler.get("eflp_done", []))}/{len(scheduler["profiles"])}')
                    else:
                        lines.append(f'Уникальных П2 завершили: {len(scheduler.get("unique_done", []))}/20')
            dust = scheduler.get('return_dust_usdt', {})
            if dust:
                lines.append('Остаток округления на П2: ' + ', '.join(
                    f'{p2_nickname(name)} {quantity} USDT' for name, quantity in dust.items()))
            if (scheduler.get('last_error') and
                    (scheduler.get('mexc_read_waiting') or scheduler.get('adspower_waiting')
                     or self.running and not cycle)):
                lines.append('⚠️ ' + scheduler['last_error'][:200])
        if cycle:
            spec = cycle['spec']
            profile = spec.get('p2_profile', 'default')
            lines.append(f"👤 Сейчас П2: {p2_nickname(profile, spec.get('nicknames', {}).get('p2'))}")
            if scheduler and scheduler.get('mode') == 'cash_volume' and spec.get('cash_policy') == 'rolling24_p1':
                from volume_policy import rolling_cash_purchases
                try:
                    window = rolling_cash_purchases(self.journal, profile,
                                                    member_id=spec.get('members', {}).get('p2'))
                except (ValueError, TypeError):
                    lines.append('⚠️ Не удалось сверить покупки П2 за 24 часа; новый ордер не откроется')
                else:
                    lines.append(f"Покупки П2 за 24 ч: {window['quantity']} / 69000 USDT (макс. 70000)")
                    if window.get('uncertain_until'):
                        lines.append('⚠️ Есть первая сделка без подтверждённого итога; новые покупки этого П2 ждут сверки')
            elif scheduler and scheduler.get('mode') in {'volume', 'cash_volume'}:
                window = scheduler.get('volume_windows', {}).get(profile, {})
                lines.append(f"Объём П2: {window.get('quantity', '0')} / 70000 USDT")
            event = self.journal.db.execute('SELECT * FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT 1',
                                            (cycle['id'],)).fetchone()
            if event:
                label = next((step.label for step in steps_for_spec(spec) if step.key == event['step']),
                             'Сверка сохранённого цикла' if event['step'] == 'cycle' else event['step'])
                if event['status'] in {'error', 'paused', 'rejected', 'unknown'}:
                    lines.append(f"⚠️ {label}: {event['message'].splitlines()[0][:180]}")
                elif event['status'] in {'pending', 'in_flight', 'waiting'}:
                    lines.append(f'➡️ Сейчас: {label}')
                else:
                    lines.append(f'✅ Последнее действие: {label}')
                leg = event['step'].split('_', 1)[0]
                if leg in {'forward', 'reverse'}:
                    created = self.journal.step(cycle['id'], leg + '_create')
                    order_no = (created or {}).get('result', {}).get('order_no')
                    if order_no:
                        lines.append(f'Ордер: {order_no}')
        elif scheduler and scheduler.get('mode') == 'single' and scheduler.get('selected'):
            lines.append(f"👤 Выбран П2: {p2_nickname(scheduler['selected'])}")
        elif self.running and scheduler and scheduler.get('last_error'):
            lines.append('⏳ Повторная попытка подключения выполняется автоматически')
        elif self.running and scheduler and scheduler.get('status') == 'waiting':
            lines.append('⏳ Ожидание следующего доступного П2')
        elif self.running:
            lines.append('⏳ Подготовка следующего цикла')
        if scheduler:
            now = datetime.now(timezone.utc)
            timers = []
            for name, limit in scheduler.get('cooldowns', {}).items():
                streak = scheduler.get('ad_rejection_streaks', {}).get(name, 0)
                detail = f' | 85010 подряд: {streak}' if limit.get('reason') == '85010' else ''
                if limit.get('manual_block'):
                    timers.append(f'{p2_nickname(name)} — проверь лимит MEXC')
                else:
                    until = datetime.fromisoformat(limit['until'])
                    if until > now:
                        timers.append(f'{p2_nickname(name)} — до {until.astimezone(KRASNOYARSK):%d.%m %H:%M}{detail}')
            if timers:
                lines += ['', '⏳ Таймеры (Красноярск):', *timers]
        return '\n'.join(lines)

    def new_args(self):
        from main import build_parser
        return build_parser().parse_args(['cycle', '--auto', '--p2-profile', self.p2_profile])

    async def launch_trade_mode(self, mode: str, p1_profile: str, p2_profiles: list[str]):
        from rollover import load_state
        from trade_modes import begin_mode
        saved = load_state(self.journal)
        if self.running or self.unfinished() or (saved and saved['status'] not in {'done', 'stopped'}):
            return await self.reply('Сначала заверши или продолжи сохранённую серию.')
        state = begin_mode(self.journal, mode, p1_profile=p1_profile,
                           p2_profiles=p2_profiles)
        self.menu = None
        self.cash_p1 = None
        self.cash_p2.clear()
        self.selected_mode = None
        self.nonce = secrets.token_hex(6)
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self.work_rollover(state))
        labels = {'cash_volume': 'Объём наличка', 'eflp_volume': 'Объём Eflp',
                  'eflp_unique': 'Уникальные Eflp'}
        from trade_profiles import profile_from_env
        p1_name = profile_from_env(p1_profile, os.environ).nickname
        return await self.reply(f'Запускаю «{labels[mode]}». П1: {p1_name}. П2 по очереди: '
                                + ', '.join(p2_nickname(name) for name in p2_profiles) + '.')

    async def perform(self, action: str, *, callback_message_id: int | None = None):
        if action.startswith('chat_'):
            return await self.recover_chat(action)
        if action == 'back':
            self.menu = None
            self.timer_target = None
            self.unique_p1 = None
            self.unique_p2.clear()
            self.cash_p1 = None
            self.cash_p2.clear()
            self.selected_mode = None
            return await self.reply('Главное меню.')
        if action in {'volume', 'unique', 'unique_start'}:
            return await self.reply('Этот старый режим больше не запускается. Выбери один из трёх режимов в меню.')
        if action == 'cash_volume':
            from trade_modes import configured_mode_profiles
            p1, chosen = configured_mode_profiles('cash_volume')
            return await self.launch_trade_mode('cash_volume', p1, chosen)
        if action in {'eflp_volume', 'eflp_unique'}:
            from rollover import load_state, profiles_from_env
            from trade_profiles import eflp_p1_profiles, ready_p1_profiles, profile_prefix
            from trade_modes import configured_mode_profiles
            saved = load_state(self.journal)
            if self.running or self.unfinished() or (saved and saved['status'] not in {'done', 'stopped'}):
                return await self.reply('Сначала заверши или продолжи сохранённую серию.')
            fixed_p2 = set(configured_mode_profiles(action)[1])
            choices = [item for item in ready_p1_profiles(eflp_p1_profiles(os.environ), os.environ,
                                                         include_main=False)
                       if item.key not in fixed_p2
                       and os.getenv(f'{profile_prefix(item.key)}_BUY_ADV_NO', '').strip()]
            if not choices:
                return await self.reply('Нет настроенного П1 с объявлениями продажи и покупки USDT, '
                                        'профилем AdsPower и вне списка П2.')
            self.menu = 'cash_p1'
            self.selected_mode = action
            self.cash_p1 = None
            self.cash_p2.clear()
            labels = {'cash_volume': 'Объём наличка', 'eflp_volume': 'Объём Eflp',
                      'eflp_unique': 'Уникальные Eflp'}
            source = ('EFLP_VOLUME_P2_PROFILES' if action == 'eflp_volume'
                      else 'EFLP_UNIQUE_P2_PROFILES')
            suffix = f' П2 возьму из {source} в .env и сразу запущу серию.'
            return await self.reply(f'Выбери П1 для режима «{labels[action]}».{suffix}')
        if action.startswith('cash_p1:'):
            from rollover import profiles_from_env
            from trade_profiles import eflp_p1_profiles, ready_p1_profiles, profile_prefix
            name = action.split(':', 1)[1]
            p1_keys = (eflp_p1_profiles(os.environ)
                       if self.selected_mode in {'eflp_volume', 'eflp_unique'}
                       else ['p1', *profiles_from_env()])
            ready = {item.key for item in ready_p1_profiles(p1_keys, os.environ, include_main=False)
                     if os.getenv(f'{profile_prefix(item.key)}_BUY_ADV_NO', '').strip()}
            if self.selected_mode in {'eflp_volume', 'eflp_unique'}:
                from trade_modes import configured_mode_profiles
                fixed_p2 = configured_mode_profiles(self.selected_mode)[1]
                ready -= set(fixed_p2)
            if self.menu != 'cash_p1' or self.running or name not in ready:
                return await self.reply('Этот П1 недоступен; открой выбор заново.')
            if self.selected_mode in {'eflp_volume', 'eflp_unique'}:
                return await self.launch_trade_mode(self.selected_mode, name, fixed_p2)
            self.cash_p1 = name
            self.cash_p2.clear()
            self.menu = 'cash_p2'
            return await self.reply('Теперь выбери профили П2. Повторное нажатие снимает выбор.')
        if action.startswith('cash_p2:'):
            from rollover import profiles_from_env
            from trade_profiles import profile_from_env, profile_prefix
            name = action.split(':', 1)[1]
            if (self.menu != 'cash_p2' or self.running or name == self.cash_p1
                    or name not in profiles_from_env()):
                return await self.reply('Этот П2 недоступен для выбора.')
            if self.selected_mode == 'cash_volume':
                profile_from_env(name, os.environ)
            else:
                prefix = profile_prefix(name)
                if not all(os.getenv(f'{prefix}_{field}', '').strip() for field in
                           ('API_KEY', 'SECRET_KEY', 'MEMBER_ID', 'NICKNAME', 'PAYMENT_ID')):
                    return await self.reply('Для этого П2 не заполнены API, ник или реквизиты оплаты.')
            if name in self.cash_p2:
                self.cash_p2.remove(name)
            else:
                self.cash_p2.add(name)
            message = f'Выбрано П2: {len(self.cash_p2)}.'
            if callback_message_id is not None:
                try:
                    await self.telegram.request('editMessageText', {
                        'chat_id': self.telegram.chat_id, 'message_id': callback_message_id,
                        'text': message, 'reply_markup': self.keyboard()})
                    return
                except RuntimeError:
                    pass
            return await self.reply(message)
        if action == 'cash_start':
            from rollover import load_state, profiles_from_env
            saved = load_state(self.journal)
            minimum = 20 if self.selected_mode == 'eflp_unique' else 1
            if (self.menu != 'cash_p2' or not self.cash_p1 or len(self.cash_p2) < minimum
                    or self.running or self.unfinished()
                    or (saved and saved['status'] not in {'done', 'stopped'})):
                return await self.reply(f'Сначала выбери П1 и не менее {minimum} П2; проверь, что нет активной серии.')
            ordered = [name for name in profiles_from_env() if name in self.cash_p2]
            mode = self.selected_mode
            return await self.launch_trade_mode(mode, self.cash_p1, ordered)
        if action == 'unique':
            if self.running or self.unfinished():
                return await self.reply('Сначала заверши или продолжи текущий цикл.')
            from rollover import profiles_from_env
            from trade_profiles import profile_from_env
            ready = 0
            for name in profiles_from_env():
                try:
                    profile_from_env(name, os.environ)
                    ready += 1
                except ValueError:
                    pass
            if ready < 20:
                return await self.reply(f'Режим «Уникальные» пока не готов: настроено {ready} П2 из необходимых 20. '
                                        'Для каждого добавь API, свой SELL_ADV_NO и ADSPOWER_PROFILE_ID в .env.')
            self.menu = 'unique_p1'
            self.unique_p1 = None
            self.unique_p2.clear()
            return await self.reply('Выбери П1. Покажу только профили с API, объявлением ПРОДАЖИ USDT и AdsPower.')
        if action.startswith('unique_p1:'):
            if self.menu != 'unique_p1' or self.running:
                return await self.reply('Выбор устарел. Открой режим «Уникальные» заново.')
            from trade_profiles import ready_p1_profiles
            from rollover import profiles_from_env
            name = action.split(':', 1)[1]
            ready = {profile.key for profile in ready_p1_profiles(profiles_from_env(), os.environ)}
            if name not in ready:
                return await self.reply('Этот П1 не настроен для запуска.')
            self.unique_p1 = name
            self.unique_p2.clear()
            self.menu = 'unique_p2'
            return await self.reply('Выбери не меньше 20 разных П2 кнопками ниже. Повторное нажатие снимает выбор.')
        if action.startswith('unique_p2:'):
            from rollover import profiles_from_env
            from trade_profiles import profile_from_env
            name = action.split(':', 1)[1]
            if (self.menu != 'unique_p2' or self.running or name == self.unique_p1
                    or name not in profiles_from_env()):
                return await self.reply('Профиль П2 недоступен для выбора.')
            profile_from_env(name, os.environ)
            if name in self.unique_p2:
                self.unique_p2.remove(name)
            else:
                self.unique_p2.add(name)
            selection = f'Выбрано П2: {len(self.unique_p2)}. Нужно не менее 20.'
            if callback_message_id is not None:
                try:
                    await self.telegram.request('editMessageText', {
                        'chat_id': self.telegram.chat_id, 'message_id': callback_message_id,
                        'text': selection, 'reply_markup': self.keyboard()})
                    return
                except RuntimeError:
                    pass
            return await self.reply(selection)
        if action in {'volume', 'unique_start'}:
            if self.running or self.unfinished():
                return await self.reply('Сначала заверши или продолжи текущий цикл.')
            from rollover import load_state, profiles_from_env
            from trade_modes import begin_mode
            previous = load_state(self.journal)
            if previous and previous['status'] not in {'done', 'stopped'}:
                return await self.reply('Уже есть сохранённая серия; нажми «Продолжить» или заверши её.')
            if action == 'unique_start':
                if self.menu != 'unique_p2' or not self.unique_p1:
                    return await self.reply('Сначала выбери П1 и П2.')
                ordered = [name for name in profiles_from_env() if name in self.unique_p2]
                state = begin_mode(self.journal, 'unique', p1_profile=self.unique_p1,
                                   p2_profiles=ordered)
            else:
                state = begin_mode(self.journal, 'volume')
            self.menu = None
            self.unique_p1 = None
            self.unique_p2.clear()
            self.nonce = secrets.token_hex(6)
            self.stop_event = asyncio.Event()
            self.task = asyncio.create_task(self.work_rollover(state))
            mode_name = 'Объём' if state['mode'] == 'volume' else 'Уникальные'
            return await self.reply(f'Запускаю режим «{mode_name}»: {len(state["profiles"])} профилей П2.')
        if action == 'timer_cancel':
            self.menu = None
            self.timer_target = None
            return await self.reply('Сброс таймера отменён.')
        if action == 'timers' or action.startswith(('timer:', 'timer_confirm:', 'unblock:')):
            if self.running:
                return await self.reply('Сначала нажми «Остановить» и дождись остановки текущего действия.')
            from rollover import load_state, clear_cooldown
            scheduler = load_state(self.journal)
            if not scheduler:
                return await self.reply('Сохранённых таймеров П2 нет.')
            if action == 'timers':
                self.menu = 'timer_profiles'
                self.timer_target = None
                return await self.reply('Выбери П2, которому нужно сбросить локальный таймер.')
            if action.startswith('timer_confirm:'):
                name = action.split(':', 1)[1]
                if self.menu != 'timer_confirm' or self.timer_target != name:
                    self.menu = None
                    self.timer_target = None
                    return await self.reply('Подтверждение устарело. Открой «Таймеры П2» заново.')
                clear_cooldown(self.journal, name)
                self.menu = None
                self.timer_target = None
                return await self.reply(f'Таймер П2 {p2_nickname(name)} сброшен в боте. '
                                        'Лимит на MEXC не изменился; для запуска нажми «Продолжить».')
            name = action.split(':', 1)[1]
            limit = scheduler.get('cooldowns', {}).get(name)
            if name not in scheduler.get('profiles', []) or not limit:
                return await self.reply('У этого П2 нет сохранённого таймера.')
            self.menu = 'timer_confirm'
            self.timer_target = name
            until = datetime.fromisoformat(limit['until']).astimezone(KRASNOYARSK)
            return await self.reply(f'Сбросить только локальный таймер П2 {p2_nickname(name)} '
                                    f'(до {until:%d.%m %H:%M} Красноярск)? '
                                    'Ограничение на MEXC останется; если оно ещё действует, новый ордер снова получит отказ 60085.')
        if action == 'status':
            return await self.reply(self.status())
        if action == 'stats':
            return await self.reply(daily_stats(self.journal))
        if action == 'add_profile':
            if str(self.owner_id) != str(self.telegram.chat_id):
                return await self.reply('Добавление API-ключей доступно только в личном чате с ботом.')
            return await self.reply('Отправь одним сообщением:\n'
                '/addprofile ИМЯ API_KEY SECRET_KEY MEMBER_ID НИК PAYMENT_ID [PROXY_URL]\n'
                'Сообщение с ключами бот сразу удалит. Не отправляй эту команду в группу. '
                'PAYMENT_ID — ID реквизитов П2, а не код метода 578. '
                'Прокси — последний параметр, например http://login:password@host:8080 '
                'или socks5://login:password@host:1080. Если его не указать, П2 подключится напрямую.')
        if action == 'reset_cancel':
            self.reset_target = None
            return await self.reply('Сброс отменён.')
        if action == 'reset' or action.startswith('reset_confirm:'):
            if self.running:
                return await self.reply('Сначала нажми «Остановить» и дождись остановки текущего действия.')
            pending = self.unfinished()
            if not pending:
                self.reset_target = None
                return await self.reply('Незавершённого цикла нет.')
            from rollover import load_state, save_state
            scheduler = load_state(self.journal)
            return_pending = ((scheduler or {}).get('pending_return') or {})
            if return_pending and return_pending.get('cycle_id') != pending['id']:
                return await self.reply('Сохранённый возврат относится к другому циклу; сброс остановлен.')
            if action == 'reset':
                self.reset_target = pending['id']
                if return_pending:
                    return await self.reply(
                        f"Сбросить цикл {pending['id']} и остановить автоматический возврат USDT? "
                        f"П2: {p2_nickname(return_pending['profile'])}; "
                        f"этап: {return_pending.get('stage', 'подготовка')}; "
                        f"количество: {return_pending.get('quantity', 'неизвестно')} USDT. "
                        'Состояние возврата останется в журнале, но бот больше не проверит вывод, '
                        'зачисление на П1 и пополнение объявления по этому циклу. '
                        'Сначала сверь вывод на MEXC. Сброс не переводит и не отменяет средства.')
                orders = [self.journal.step(pending['id'], leg + '_create') for leg in ('forward', 'reverse')]
                order_ids = ', '.join(step['result']['order_no'] for step in orders
                                      if step and step['result'].get('order_no')) or 'нет сохранённых номеров'
                return await self.reply(f"Сбросить цикл {pending['id']}? Ордера MEXC: {order_ids}. "
                                        'Сброс удалит его только из очереди программы; сделки и средства на MEXC не изменятся. '
                                        'Проверь ордера, затем нажми «Подтвердить сброс» или «Отмена».')
            cid = action.split(':', 1)[1]
            if cid != self.reset_target or cid != pending['id']:
                self.reset_target = None
                return await self.reply('Подтверждение устарело. Открой сброс заново.')
            if return_pending:
                from rollover import reset_with_pending_return
                reset_with_pending_return(self.journal, scheduler, cid)
                self.reset_target = None
                return await self.reply(
                    f'Цикл {cid} сброшен локально. Незавершённый возврат сохранён в журнале; '
                    'бот больше не продолжит его автоматически. Проверь средства на MEXC.')
            self.journal.abandon(cid)
            if scheduler and scheduler.get('active_cycle') == cid:
                scheduler['active_cycle'] = None
                scheduler['status'] = 'stopped'
                from rollover import cycle_rowid
                scheduler['last_cycle_rowid'] = cycle_rowid(self.journal, cid)
                save_state(self.journal, scheduler)
            self.reset_target = None
            return await self.reply(f'Цикл {cid} сброшен в программе. Ордера MEXC не отменены. Можно начать новую серию.')
        if action == 'profiles':
            self.menu = 'start_profiles'
            return await self.reply('Выбери профиль П2 кнопкой ниже. Он будет работать до остановки или лимита MEXC.')
        if action == 'switch':
            self.menu = 'switch_profiles'
            return await self.reply('Для сохранённой серии выбери профиль кнопкой «⇄ имя» ниже. Активный ордер П2 не меняется.')
        if action.startswith('select:'):
            if self.running:
                return await self.reply('Сначала останови серию.')
            from rollover import load_state, profiles_from_env, save_state
            name = action.split(':', 1)[1]
            state = load_state(self.journal)
            if not state or state['status'] == 'done' or name not in profiles_from_env() or name not in state['profiles']:
                return await self.reply('Нет серии для смены П2. Запусти новую серию.')
            if state.get('active_cycle') or state.get('pending_return'):
                return await self.reply('Сначала заверши текущий цикл или возврат средств; его П2 закреплён в журнале.')
            state['cursor'] = state['profiles'].index(name)
            if state['mode'] == 'single':
                state['selected'] = name
            save_state(self.journal, state)
            self.menu = None
            return await self.reply(f'Следующий П2: {p2_nickname(name)}. Таймер профиля сохраняется; нажми «Продолжить».')
        if action == 'finish':
            if self.running:
                return await self.reply('Сначала останови серию.')
            from rollover import finish_series
            finish_series(self.journal)
            return await self.reply('Серия завершена в программе. Таймеры профилей сохранены.')
        if action == 'stop':
            if not self.running:
                pending = self.unfinished()
                if pending:
                    return await self.reply(f"Цикл {pending['id']} уже остановлен. Для продолжения нажми «Продолжить».")
                return await self.reply('Бот уже остановлен. Прогресс сохранён.')
            self.stop_event.set()
            return await self.reply('Остановка запрошена. Пауза прервётся сразу; отправленная операция завершится и сохранится. Следующий шаг не начнётся. Ордера MEXC не отменяются.')
        from rollover import begin, load_state, profiles_from_env
        scheduler = load_state(self.journal)
        if action not in {'new', 'resume', 'all'} and not action.startswith('single:'):
            return
        if self.running:
            return await self.reply('Уже выполняется цикл. Сначала нажми «Остановить».')
        if action in {'all'} or action.startswith('single:'):
            self.menu = None
            chosen = action.split(':', 1)[1] if action.startswith('single:') else None
            pending = self.unfinished()
            if pending:
                saved_profile = pending['spec'].get('p2_profile', 'default')
                if chosen == saved_profile and (not scheduler or scheduler['status'] in {'done', 'stopped'}):
                    return await self.perform('resume')
                return await self.reply(f"Есть незавершённый цикл {pending['id']} с П2 {p2_nickname(saved_profile, pending['spec'].get('nicknames', {}).get('p2'))}. "
                                        'Нажми «Продолжить»; новый профиль можно выбрать после его завершения.')
            state = begin(self.journal, 'single' if chosen else 'all', chosen)
            self.reset_target = None
            self.nonce = secrets.token_hex(6)
            self.stop_event = asyncio.Event()
            self.task = asyncio.create_task(self.work_rollover(state))
            return await self.reply(f"Запускаю {state['mode']} без ограничения числа циклов; П2: {', '.join(p2_nickname(name) for name in state['profiles'])}. Остановка — кнопкой в боте.")
        if action == 'resume' and scheduler and scheduler['status'] not in {'done', 'stopped'}:
            self.menu = None
            self.reset_target = None
            self.nonce = secrets.token_hex(6)
            self.stop_event = asyncio.Event()
            self.task = asyncio.create_task(self.work_rollover(scheduler))
            return await self.reply('Продолжаю сохранённую серию профилей и её текущий этап.')
        args = self.new_args()
        if action == 'new':
            self.journal.ensure_can_create()
            plan = auto_plan(args, os.environ)
            description = f"Запускаю серию: {plan['count']} циклов, {plan['min_amount']}–{plan['max_amount']} {plan['fiat']}; П2 {self.p2_profile}."
        else:
            cycle = self.unfinished()
            if not cycle or cycle['status'] == 'abandoned':
                return await self.reply('Нет незавершённого цикла. Выбери «Все профили» или «Один профиль».')
            spec = cycle['spec']
            if not spec.get('automatic') or spec.get('mode') != 'api':
                return await self.reply('Этот цикл требует подтверждений в консоли. Через Telegram продолжаются только автоматические циклы.')
            series = spec.get('series')
            refill = self.journal.step(cycle['id'], 'reverse_replenish')
            if cycle['status'] == 'completed' and refill and refill['status'] == 'done' and (
                    not series or series['index'] >= series['count']):
                return await self.reply('Цикл и серия уже завершены. Можно начать новый запуск.')
            args.resume, args.p2_profile = cycle['id'], None
            series_label = (f"; серия {series['index']} из {series['count']}" if series else '')
            description = (f"Продолжаю цикл {cycle['id']}{series_label} "
                           f"с сохранённым П2 {p2_nickname(spec.get('p2_profile', 'default'), spec.get('nicknames', {}).get('p2'))}.")
        # Invalidate old buttons before starting; duplicate clicks cannot launch another series.
        self.reset_target = None
        self.nonce = secrets.token_hex(6)
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self.work(args))
        await self.reply(description)

    async def work_rollover(self, state):
        from rollover import run
        before = self.journal.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        try:
            await run(self.journal, state, self.stop_event, self.telegram,
                      telegram_keyboard=lambda: self.keyboard(force_idle=True))
        except OperatorStopped:
            if not self.shutdown_event.is_set():
                await self.reply('⏹ Серия остановлена. Таймеры и этапы сохранены; нажми «Продолжить».')
        except Exception as exc:
            from mexc_client import MexcAPIError
            from adspower import AdsPowerError
            reason = str(exc) if isinstance(exc, (Paused, ValueError, MexcAPIError, AdsPowerError)) else type(exc).__name__
            self.logger.error('Rollover worker stopped: %s', reason[:800])
            reported = self.journal.db.execute(
                "SELECT 1 FROM events WHERE id>? AND status IN ('error','paused') LIMIT 1", (before,)).fetchone()
            pending_return = state.get('pending_return') or {}
            if pending_return and pending_return.get('stage') not in {None, 'done'}:
                await self.reply(
                    f"❌ Возврат USDT после лимита остановлен.\n"
                    f"П2: {p2_nickname(pending_return['profile'])}\n"
                    f"Цикл: {pending_return['cycle_id']}\n"
                    f"Этап: {pending_return['stage']}\n"
                    f"Ошибка: {reason[:600]}\n"
                    "Повторного вывода не было. Сверь историю вывода MEXC перед продолжением.",
                    force_idle=True)
            elif not reported:
                await self.reply(f'❌ Серия остановлена: {reason[:800]}\nСтатус и этап сохранены; проверь MEXC перед продолжением.',
                                 force_idle=True)

    async def work(self, args):
        before = self.journal.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        try:
            await run_command(args, stop_event=self.stop_event, use_lock=False,
                              notify_prepare_errors=False, telegram_keyboard=lambda: self.keyboard(force_idle=True))
        except Exception as exc:
            from adspower import AdsPowerError
            from mexc_client import MexcAPIError
            reason = str(exc) if type(exc) in {ValueError, RuntimeError} or isinstance(exc, (AdsPowerError, MexcAPIError)) else type(exc).__name__
            self.logger.error('Cycle worker failed: %s', reason[:800])
            # Normal cycle failures already have a durable Telegram outbox event.
            reported = self.journal.db.execute("SELECT 1 FROM events WHERE id>? AND status IN ('error','paused') LIMIT 1", (before,)).fetchone()
            if not reported:
                await self.reply(f'❌ Запуск не выполнен: {reason[:800]}', force_idle=True)
        finally:
            if self.stop_event.is_set():
                await self.reply('⏹ Выполнение остановлено. Прогресс сохранён; для продолжения нажми «Продолжить».')

    async def handle(self, update: dict):
        update_id = update.get('update_id')
        if type(update_id) is not int or update_id < self.offset:
            return
        # Record before dispatch: a crash must never replay a start command.
        self.offset = update_id + 1
        with self.journal.db:
            self.journal.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (self.offset_key, str(self.offset)))
        callback = update.get('callback_query')
        source = callback if isinstance(callback, dict) else update.get('message', {})
        message = source.get('message', {}) if callback else source
        user = source.get('from', {})
        authorized = (str(message.get('chat', {}).get('id')) == str(self.telegram.chat_id)
                      and user.get('id') == self.owner_id and not user.get('is_bot', False))
        if callback:
            data = callback.get('data', '')
            valid = authorized and isinstance(data, str) and data.startswith(self.nonce + ':')
            await self.telegram.request('answerCallbackQuery', {'callback_query_id': callback['id'],
                'text': '' if valid else 'Кнопка недоступна. Открой меню командой /start.'})
            if not valid:
                return
            try:
                action = data.split(':', 1)[1]
                if type(message.get('message_id')) is int:
                    await self.perform(action, callback_message_id=message['message_id'])
                else:
                    await self.perform(action)
            except (ValueError, RuntimeError) as exc:
                await self.reply('❌ ' + str(exc)[:800])
        elif authorized and source.get('text', '').split('@')[0].strip() in {'/start', '/menu'}:
            await self.reply(self.status())
        elif authorized and isinstance(source.get('text'), str) and source['text'].startswith('/addprofile'):
            if str(self.owner_id) != str(self.telegram.chat_id) or self.running:
                return await self.reply('Останови цикл и добавь профиль только в личном чате с ботом.')
            try:
                deleted = await self.telegram.request('deleteMessage',
                    {'chat_id': self.telegram.chat_id, 'message_id': source['message_id']})
                if deleted is not True:
                    raise ValueError('Не удалось удалить сообщение с ключами; профиль не сохранён')
            except (KeyError, RuntimeError, ValueError):
                return await self.reply('Не удалось удалить сообщение с ключами; профиль не сохранён. '
                                        'Удали сообщение вручную и проверь права бота.')
            from profile_registry import add_profile
            try:
                values = source['text'].split()[1:]
                name = add_profile(PROJECT_DIR / '.env', values)
                connection = 'Прокси сохранён для API и чата.' if len(values) == 7 else 'Прокси не задан; подключение прямое.'
                await self.reply(f'П2 {name} добавлен. {connection} Профиль доступен для нового цикла; текущий П2 не меняется.')
            except (ValueError, OSError) as exc:
                await self.reply('Профиль не добавлен: ' + str(exc)[:350])
        elif authorized and isinstance(source.get('text'), str) and source['text'].startswith('/bindtran '):
            if self.running:
                return await self.reply('Останови серию перед привязкой номера перевода.')
            from rollover import load_state
            from return_funds import bind_transfer
            try:
                await bind_transfer(self.journal, load_state(self.journal), source['text'].split(maxsplit=1)[1].strip())
                await self.reply('Номер перевода сверен с MEXC и сохранён. Нажми «Продолжить».')
            except (ValueError, RuntimeError) as exc:
                await self.reply('❌ ' + str(exc)[:700])

    async def listen(self):
        from adspower import AdsPowerError

        reporter = Reporter(self.journal, self.telegram, self.sheets,
                            keyboard=lambda: self.keyboard(force_idle=True))
        browser_guard = getattr(self, 'browser_guard', None)
        profile_check_at = 0.0
        profile_check_failed = False
        resumed = self.resume_after_restart()
        await self.reply(('Управление включено. Продолжаю серию после перезапуска.\n' if resumed else
                          'Управление включено. Сделки сами не запускаются — выбери действие.\n')
                         + self.status())
        try:
            while not self.shutdown_event.is_set():
                try:
                    operation = 'profile check'
                    now = asyncio.get_running_loop().time()
                    if (browser_guard and browser_guard.api_key and browser_guard.profile_id
                            and now >= profile_check_at):
                        profile_check_at = now + 60
                        try:
                            protected = set()
                            from rollover import load_state
                            scheduler = load_state(self.journal)
                            if scheduler and scheduler.get('mode') in {'volume', 'unique', 'cash_volume',
                                                                          'eflp_volume', 'eflp_unique'}:
                                from trade_profiles import profile_prefix
                                p1_key = scheduler.get('p1_profile', 'p1')
                                if p1_key != 'p1':
                                    protected.add(os.getenv(profile_prefix(p1_key) + '_ADSPOWER_PROFILE_ID', ''))
                                cycle = self.unfinished()
                                p2_key = (cycle['spec']['p2_profile']
                                          if cycle and cycle['spec'].get('reverse_maker') == 'p2'
                                          else scheduler.get('current_profile'))
                                if p2_key:
                                    protected.add(os.getenv(profile_prefix(p2_key) + '_ADSPOWER_PROFILE_ID', ''))
                            closed = (await browser_guard.close_other_local_profiles(protected - {''})
                                      if protected else await browser_guard.close_other_local_profiles())
                            if closed:
                                self.logger.info('AdsPower: closed %s non-P1 profiles', closed)
                            profile_check_failed = False
                        except AdsPowerError as exc:
                            if not profile_check_failed:
                                self.logger.warning('AdsPower profile check failed (%s)', type(exc).__name__)
                                await self.reply('⚠️ Не удалось проверить открытые профили AdsPower. '
                                                 'Проверь Local API; бот не закрыл другие профили.')
                            profile_check_failed = True
                    if not self.running:
                        operation = 'notification delivery'
                        await reporter.flush()
                    operation = 'getUpdates'
                    updates = await self.telegram.request('getUpdates', {'offset': self.offset, 'timeout': 20,
                        'allowed_updates': ['message', 'callback_query']}, timeout=30)
                    for update in updates:
                        operation = 'update handling'
                        await self.handle(update)
                except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                    detail = str(exc) if isinstance(exc, TelegramRequestError) else type(exc).__name__
                    self.logger.warning('Telegram control %s failed: %s', operation, detail)
                    await asyncio.sleep(5)
        finally:
            self.stop_event.set()
            if self.running:
                await asyncio.shield(self.task)


async def serve(args):
    from logger_setup import setup_logging
    settings = Settings.from_env(require_keys=False)
    setup_logging(settings.log_dir, settings.log_level)
    telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    if not telegram.enabled:
        raise ValueError('Для управления заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID')
    chat_id = int(settings.telegram_chat_id)
    owner_id = int(os.getenv('TELEGRAM_CONTROL_USER_ID') or (chat_id if chat_id > 0 else 0))
    if owner_id <= 0:
        raise ValueError('Для управления из группы задайте TELEGRAM_CONTROL_USER_ID — личный Telegram ID оператора')
    profile = select_p2_profile(args.p2_profile, None, os.environ)
    sheet_id = os.getenv('GOOGLE_SHEET_ID', '').strip()
    credentials = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip()
    if bool(sheet_id) != bool(credentials):
        raise ValueError('Заполните вместе GOOGLE_SHEET_ID и GOOGLE_SERVICE_ACCOUNT_FILE')
    sheets = None
    if sheet_id:
        credentials_path = Path(credentials)
        if not credentials_path.is_absolute():
            credentials_path = PROJECT_DIR / credentials_path
        if not credentials_path.is_file():
            raise ValueError('Файл сервисного аккаунта Google не найден')
        sheets = GoogleSheets(sheet_id, os.getenv('GOOGLE_SHEET_TAB', 'Продажи'), str(credentials_path))
    path = Path(os.getenv('CYCLE_DB', 'data/cycles.sqlite3'))
    if not path.is_absolute():
        path = PROJECT_DIR / path
    # Own the trading lock for the listener's lifetime; no competing console runner.
    with process_lock(path.with_suffix('.lock')):
        journal = Journal(path)
        try:
            control = TelegramControl(journal, telegram, owner_id, profile, sheets)
            from adspower import AdsPower
            control.browser_guard = AdsPower.from_env()
            if os.name != 'nt':
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGTERM, control.request_shutdown)
            print('Telegram: управление запущено. Откройте чат с ботом и отправьте /start. Ctrl+C — остановка.')
            await control.listen()
        finally:
            if os.name != 'nt':
                asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
            journal.close()
    return 0
