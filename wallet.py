"""Standalone, confirmed OTC/SPOT transfers. Never retries a money-moving request."""
from decimal import Decimal
import json
import os
from pathlib import Path
import uuid

from config import PROJECT_DIR, Settings, select_p2_profile
from cycle import Console, OperatorStopped, fingerprint, money
from journal import Journal, now, process_lock
from mexc_client import MexcP2PClient


class WalletTransfers:
    def __init__(self, journal, console):
        self.db, self.console = journal.db, console
        self.db.execute("""CREATE TABLE IF NOT EXISTS wallet_transfers (
            id TEXT PRIMARY KEY, created TEXT NOT NULL, spec TEXT NOT NULL,
            status TEXT NOT NULL, tran_id TEXT UNIQUE)""")

    def get(self, transfer_id):
        row = self.db.execute('SELECT * FROM wallet_transfers WHERE id=?', (transfer_id,)).fetchone()
        if not row:
            raise ValueError('Перевод не найден в местном журнале')
        return dict(row) | {'spec': json.loads(row['spec'])}

    async def send(self, client, spec):
        for row in self.db.execute("SELECT * FROM wallet_transfers WHERE status NOT IN ('SUCCESS', 'FAILED', 'NOT_SENT')"):
            saved = json.loads(row['spec'])
            if (saved['key_hash'] == spec['key_hash'] or
                    (saved['account'], saved['p2_profile']) == (spec['account'], spec['p2_profile'])):
                raise RuntimeError(f"Есть несверенный перевод {row['id']}. Сначала выполните wallet-transfer-status {row['id']}")
        profile = f" / профиль {spec['p2_profile']}" if spec['account'] == 'p2' else ''
        self.console.confirm(f"{spec['account'].upper()}{profile}: {spec['amount']} USDT, "
                             f"{spec['source']} → {spec['target']}. Перевод внутри одного аккаунта MEXC.")
        transfer_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO wallet_transfers(id,created,spec,status,tran_id) VALUES (?,?,?,'in_flight',NULL)",
                            (transfer_id, now(), json.dumps(spec)))
        self.console.write(f"Операция {transfer_id} сохранена. Проверка: wallet-transfer-status {transfer_id}")
        # Commit intent before POST. A crash or lost response leaves the transfer blocked.
        tran_id = await client.transfer_usdt(spec['source'], spec['target'], spec['amount'])
        with self.db:
            self.db.execute("UPDATE wallet_transfers SET status='submitted',tran_id=? WHERE id=?", (tran_id, transfer_id))
        self.console.write(f"Запрос принят MEXC, tranId: {tran_id}. Проверяем результат.")
        return await self.check(client, transfer_id)

    def confirm_not_sent(self, transfer_id):
        saved = self.get(transfer_id)
        if saved['tran_id'] or saved['status'] != 'in_flight':
            raise ValueError('У операции уже есть результат или tranId; используйте проверку статуса')
        self.console.confirm('Проверьте историю переводов и остатки обоих счетов на MEXC. '
                             'Подтвердите, что эта попытка не создала перевод. При сомнении — STOP.',
                             f'ПЕРЕВОД НЕ СОЗДАН {transfer_id}')
        with self.db:
            self.db.execute("UPDATE wallet_transfers SET status='NOT_SENT' WHERE id=?", (transfer_id,))
        self.console.write('Отсутствие перевода подтверждено оператором. Новая заявка не отправлена.')
        return 0

    async def check(self, client, transfer_id, tran_id=None):
        saved = self.get(transfer_id)
        if saved['tran_id'] and tran_id and saved['tran_id'] != tran_id:
            raise ValueError('Нельзя заменить сохранённый tranId другим переводом')
        tran_id = saved['tran_id'] or tran_id
        if not tran_id:
            raise RuntimeError('Ответ на перевод не сохранён. Найдите tranId в истории MEXC и выполните '
                               f'wallet-transfer-status {transfer_id} --tran-id НОМЕР. Повтор не отправлен.')
        detail = await client.get_wallet_transfer(tran_id)
        spec = saved['spec']
        if (detail.get('tranId') != tran_id or detail.get('asset') != 'USDT'
                or detail.get('fromAccountType') != spec['source'] or detail.get('toAccountType') != spec['target']
                or Decimal(money(detail.get('amount'))) != Decimal(spec['amount'])):
            raise RuntimeError('Реквизиты перевода MEXC не совпали с сохранённой операцией')
        # When recovering a lost response, do not bind an older same-amount transfer.
        if not saved['tran_id']:
            from datetime import datetime
            started = datetime.fromisoformat(saved['created']).timestamp() * 1000
            timestamp = detail.get('timestamp')
            if not isinstance(timestamp, (int, float)) or not started - 60000 <= timestamp <= started + 300000:
                raise RuntimeError('Время перевода не соответствует попытке; нужна сверка истории MEXC')
        duplicate = self.db.execute('SELECT id FROM wallet_transfers WHERE tran_id=? AND id<>?', (tran_id, transfer_id)).fetchone()
        if duplicate:
            raise ValueError('Этот tranId уже привязан к другой местной операции')
        status = detail.get('status')
        if status not in {'SUCCESS', 'FAILED', 'WAIT'}:
            raise RuntimeError('Неизвестный статус перевода MEXC; повтор не отправлен')
        with self.db:
            self.db.execute('UPDATE wallet_transfers SET status=?,tran_id=? WHERE id=?', (status, tran_id, transfer_id))
        label = {'SUCCESS': 'Перевод выполнен', 'FAILED': 'Перевод отклонён MEXC', 'WAIT': 'Перевод ещё обрабатывается'}[status]
        self.console.write(f"{label}: {spec['amount']} USDT, {spec['source']} → {spec['target']}; tranId {tran_id}.")
        if status != 'SUCCESS':
            self.console.write(f'Проверка: wallet-transfer-status {transfer_id}. Автоматического повтора нет.')
        return 0 if status == 'SUCCESS' else 1


async def run_command(args, console=None):
    console = console or Console()
    path = Path(os.getenv('CYCLE_DB', 'data/cycles.sqlite3'))
    if not path.is_absolute():
        path = PROJECT_DIR / path
    # Share the cycle lock: stop the trading/Telegram worker before a manual transfer.
    with process_lock(path.with_suffix('.lock')):
        journal = Journal(path)
        client = None
        try:
            transfers = WalletTransfers(journal, console)
            if args.command == 'wallet-transfer':
                profile = select_p2_profile(args.p2_profile, None, os.environ) if args.account == 'p2' else None
                if args.account != 'p2' and args.p2_profile is not None:
                    raise ValueError('--p2-profile применяется только к --account p2')
                settings = Settings.from_env(args.account, p2_profile=profile)
                if not settings.enable_state_changes:
                    raise ValueError('Для перевода установите ENABLE_STATE_CHANGES=true в .env')
                target = {'spot': 'SPOT', 'fiat': 'OTC'}[args.to]
                spec = {'account': args.account, 'p2_profile': profile, 'key_hash': fingerprint(settings.api_key),
                        'source': 'OTC' if target == 'SPOT' else 'SPOT', 'target': target, 'amount': money(args.amount)}
            else:
                spec = transfers.get(args.transfer_id)['spec']
                settings = Settings.from_env(spec['account'], p2_profile=spec['p2_profile'])
                if fingerprint(settings.api_key) != spec['key_hash']:
                    raise ValueError('API-ключ отличается от ключа сохранённого перевода; верните исходные настройки')
            client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url, settings.recv_window)
            if args.command == 'wallet-transfer':
                return await transfers.send(client, spec)
            if args.not_sent:
                return transfers.confirm_not_sent(args.transfer_id)
            return await transfers.check(client, args.transfer_id, args.tran_id)
        except OperatorStopped:
            console.write('Перевод не отправлен.')
            return 0
        finally:
            if client:
                await client.close()
            journal.close()
