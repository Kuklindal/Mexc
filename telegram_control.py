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
from cycle import STEPS, OperatorStopped, Paused, auto_plan, run_command
from journal import Journal, process_lock
from notifier import TelegramNotifier
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
            rows.append((('🔄 Все профили', 'all'), ('👤 Один профиль', 'profiles')))

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
                rows.extend(tuple(('⇄ ' + p2_nickname(name), 'select:' + name) for name in names[i:i + 3])
                            for i in range(0, len(names), 3))
            rows.append((('↩️ Назад', 'back'),))
        if scheduler and (not self.running or force_idle):
            now = datetime.now(timezone.utc)
            timed = [name for name, value in scheduler.get('cooldowns', {}).items()
                     if value.get('manual_block') or datetime.fromisoformat(value['until']) > now]
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
        for step in STEPS:
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
            settings = Settings.from_env(actor, p2_profile=profile if actor == 'p2' else None)
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
        elif self.running:
            bot_state = 'Работает'
        elif cycle or (scheduler and scheduler.get('status') == 'paused'):
            bot_state = 'На паузе'
        else:
            bot_state = 'Не запущен'
        lines = [f'📍 Бот: {bot_state}']
        if scheduler:
            lines.append('Режим: ' + ('все профили' if scheduler.get('mode') == 'all' else 'один профиль'))
            lines.append(f"Завершено циклов: {scheduler.get('completed_count', 0)}")
        if cycle:
            spec = cycle['spec']
            profile = spec.get('p2_profile', 'default')
            lines.append(f"👤 Сейчас П2: {p2_nickname(profile, spec.get('nicknames', {}).get('p2'))}")
            event = self.journal.db.execute('SELECT * FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT 1',
                                            (cycle['id'],)).fetchone()
            if event:
                label = next((step.label for step in STEPS if step.key == event['step']), event['step'])
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
        elif self.running:
            lines.append('⏳ Ожидание следующего доступного П2')
        if scheduler:
            now = datetime.now(timezone.utc)
            timers = []
            for name, limit in scheduler.get('cooldowns', {}).items():
                if limit.get('manual_block'):
                    timers.append(f'{p2_nickname(name)} — проверь лимит MEXC')
                else:
                    until = datetime.fromisoformat(limit['until'])
                    if until > now:
                        timers.append(f'{p2_nickname(name)} — до {until.astimezone(KRASNOYARSK):%d.%m %H:%M}')
            if timers:
                lines += ['', '⏳ Таймеры (Красноярск):', *timers]
        return '\n'.join(lines)

    def new_args(self):
        from main import build_parser
        return build_parser().parse_args(['cycle', '--auto', '--p2-profile', self.p2_profile])

    async def perform(self, action: str):
        if action.startswith('chat_'):
            return await self.recover_chat(action)
        if action == 'back':
            self.menu = None
            self.timer_target = None
            return await self.reply('Главное меню.')
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
                await self.perform(data.split(':', 1)[1])
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
        reporter = Reporter(self.journal, self.telegram, self.sheets,
                            keyboard=lambda: self.keyboard(force_idle=True))
        resumed = self.resume_after_restart()
        await self.reply(('Управление включено. Продолжаю серию после перезапуска.\n' if resumed else
                          'Управление включено. Сделки сами не запускаются — выбери действие.\n')
                         + self.status())
        try:
            while not self.shutdown_event.is_set():
                try:
                    if not self.running:
                        await reporter.flush()
                    updates = await self.telegram.request('getUpdates', {'offset': self.offset, 'timeout': 20,
                        'allowed_updates': ['message', 'callback_query']}, timeout=30)
                    for update in updates:
                        await self.handle(update)
                except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                    self.logger.warning('Telegram control request failed (%s)', type(exc).__name__)
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
