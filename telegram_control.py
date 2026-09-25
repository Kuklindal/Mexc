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

from config import PROJECT_DIR, Settings, select_p2_profile
from cycle import STEPS, auto_plan, run_command
from journal import Journal, process_lock
from notifier import TelegramNotifier
from sheets import Reporter, krasnoyarsk_time

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
    def __init__(self, journal: Journal, telegram: TelegramNotifier, owner_id: int, p2_profile: str):
        self.journal, self.telegram = journal, telegram
        self.owner_id, self.p2_profile = owner_id, p2_profile
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.nonce = secrets.token_hex(6)
        self.offset_key = 'telegram_offset_' + hashlib.sha256(telegram.bot_token.encode()).hexdigest()[:16]
        row = journal.db.execute('SELECT value FROM meta WHERE key=?', (self.offset_key,)).fetchone()
        self.offset = int(row[0]) if row else 0
        self.logger = logging.getLogger('mexc_p2p.control')

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def keyboard(self):
        return {'inline_keyboard': [[{'text': label, 'callback_data': self.nonce + ':' + action} for label, action in row]
                for row in [(('▶️ Продолжить', 'resume'), ('⏹ Остановить', 'stop')),
                            (('🆕 Новый цикл', 'new'),),
                            (('📍 Статус', 'status'), ('📊 Сегодня', 'stats'))]]}

    async def reply(self, text):
        await self.telegram.send(text, reply_markup=self.keyboard())

    def current(self):
        rows = self.journal.cycles()
        pending = next((row for row in rows if row['status'] not in {'completed', 'abandoned'}), None)
        return self.journal.cycle((pending or rows[0])['id']) if rows else None

    def status(self):
        state = 'Останавливается после текущего шага' if self.running and self.stop_event.is_set() else (
            'Работает' if self.running else 'Не запущен')
        lines = [f'📍 Бот: {state}', f'П2 для нового запуска: {self.p2_profile}']
        cycle = self.current()
        if not cycle:
            return '\n'.join(lines + ['Циклов пока нет.'])
        cid, spec = cycle['id'], cycle['spec']
        lines += [f"Цикл: {cid} | {cycle['status']}",
                  f"П2 цикла: {spec.get('p2_profile', 'default')} / {spec.get('nicknames', {}).get('p2', '—')}"]
        if spec.get('series'):
            series = spec['series']
            lines.append(f"Серия: {series['index']} из {series['count']}")
        event = self.journal.db.execute('SELECT * FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
        if event:
            label = next((step.label for step in STEPS if step.key == event['step']), event['step'])
            lines += [f"Последний шаг: {label} ({event['status']})", event['message'][:700],
                      f"Обновлено: {krasnoyarsk_time(event['time'])}"]
        for leg, label in (('forward', 'Первая сделка'), ('reverse', 'Обратная сделка')):
            step = self.journal.step(cid, leg + '_create')
            if step and step['result'].get('order_no'):
                lines.append(f"{label}: {step['result']['order_no']}")
        lines.append('Состояние выполнения по журналу; это не отдельная проверка оплаты на бирже.')
        return '\n'.join(lines)

    def new_args(self):
        from main import build_parser
        return build_parser().parse_args(['cycle', '--auto', '--p2-profile', self.p2_profile])

    async def perform(self, action: str):
        if action == 'status':
            return await self.reply(self.status())
        if action == 'stats':
            return await self.reply(daily_stats(self.journal))
        if action == 'stop':
            if not self.running:
                return await self.reply('Бот уже остановлен. Прогресс сохранён.')
            self.stop_event.set()
            return await self.reply('Остановка запрошена. Пауза прервётся сразу; отправленная операция завершится и сохранится. Следующий шаг не начнётся. Ордера MEXC не отменяются.')
        if action not in {'new', 'resume'}:
            return
        if self.running:
            return await self.reply('Уже выполняется цикл. Сначала нажми «Остановить».')
        args = self.new_args()
        if action == 'new':
            self.journal.ensure_can_create()
            plan = auto_plan(args, os.environ)
            description = f"Запускаю серию: {plan['count']} циклов, {plan['min_amount']}–{plan['max_amount']} {plan['fiat']}; П2 {self.p2_profile}."
        else:
            cycle = self.current()
            if not cycle or cycle['status'] == 'abandoned':
                return await self.reply('Нет цикла для продолжения. Нажми «Новый цикл».')
            spec = cycle['spec']
            if not spec.get('automatic') or spec.get('mode') != 'api':
                return await self.reply('Этот цикл требует подтверждений в консоли. Через Telegram продолжаются только автоматические циклы.')
            series = spec.get('series')
            refill = self.journal.step(cycle['id'], 'reverse_replenish')
            if cycle['status'] == 'completed' and refill and refill['status'] == 'done' and (
                    not series or series['index'] >= series['count']):
                return await self.reply('Цикл и серия уже завершены. Можно начать новый запуск.')
            args.resume, args.p2_profile = cycle['id'], None
            description = f"Продолжаю цикл {cycle['id']} и оставшуюся серию с сохранённым П2 {spec.get('p2_profile', 'default')}."
        # Invalidate old buttons before starting; duplicate clicks cannot launch another series.
        self.nonce = secrets.token_hex(6)
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self.work(args))
        await self.reply(description)

    async def work(self, args):
        before = self.journal.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        try:
            await run_command(args, stop_event=self.stop_event, use_lock=False, notify_prepare_errors=False)
        except Exception as exc:
            from adspower import AdsPowerError
            from mexc_client import MexcAPIError
            reason = str(exc) if type(exc) in {ValueError, RuntimeError} or isinstance(exc, (AdsPowerError, MexcAPIError)) else type(exc).__name__
            self.logger.error('Cycle worker failed: %s', reason[:800])
            # Normal cycle failures already have a durable Telegram outbox event.
            reported = self.journal.db.execute("SELECT 1 FROM events WHERE id>? AND status IN ('error','paused') LIMIT 1", (before,)).fetchone()
            if not reported:
                await self.reply(f'❌ Запуск не выполнен: {reason[:800]}')
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
            await self.reply('Управление циклом. «Новый цикл» запускает серию по настройкам .env.\n' + self.status())

    async def listen(self):
        reporter = Reporter(self.journal, self.telegram, None)
        await self.reply('Управление включено. Сделки сами не запускаются — выбери действие.\n' + self.status())
        try:
            while True:
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
    path = Path(os.getenv('CYCLE_DB', 'data/cycles.sqlite3'))
    if not path.is_absolute():
        path = PROJECT_DIR / path
    # Own the trading lock for the listener's lifetime; no competing console runner.
    with process_lock(path.with_suffix('.lock')):
        journal = Journal(path)
        try:
            print('Telegram: управление запущено. Откройте чат с ботом и отправьте /start. Ctrl+C — остановка.')
            await TelegramControl(journal, telegram, owner_id, profile).listen()
        finally:
            journal.close()
    return 0
