"""Durable cycle state and an outbox. No credentials or verification codes here."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def process_lock(path: Path):
    """An OS lock is released even if the process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Другой процесс уже использует журнал. Закройте его и повторите.") from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cycles (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, status TEXT NOT NULL,
                spec TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS steps (
                cycle_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(cycle_id, name)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                time TEXT NOT NULL, cycle_id TEXT NOT NULL, step TEXT NOT NULL,
                actor TEXT NOT NULL, status TEXT NOT NULL, order_no TEXT NOT NULL,
                amount TEXT NOT NULL, fiat TEXT NOT NULL, quantity TEXT NOT NULL,
                message TEXT NOT NULL, telegram_sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT UNIQUE NOT NULL,
                amount TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
            );
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(sales)")}
        with self.db:
            for name in ("quantity", "completed_at"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE sales ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            self.db.execute("""UPDATE sales SET
                quantity=COALESCE((SELECT quantity FROM events WHERE events.cycle_id=sales.cycle_id
                    AND step='forward_complete' AND status='done' ORDER BY id LIMIT 1), ''),
                completed_at=COALESCE((SELECT time FROM events WHERE events.cycle_id=sales.cycle_id
                    AND step='forward_complete' AND status='done' ORDER BY id LIMIT 1), '')
                WHERE quantity='' OR completed_at=''""")
    def close(self):
        self.db.close()

    def ensure_can_create(self):
        if self.db.execute("SELECT 1 FROM cycles WHERE status NOT IN ('completed', 'abandoned')").fetchone():
            raise RuntimeError("Есть незавершённый цикл. Используйте cycle-status, cycle --resume ID или cycle-reset ID.")

    def create(self, spec: dict) -> str:
        self.ensure_can_create()
        cycle_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO cycles VALUES (?, ?, 'active', ?)", (cycle_id, now(), json.dumps(spec)))
        return cycle_id

    def abandon(self, cycle_id: str):
        if self.cycle(cycle_id)["status"] in {"completed", "abandoned"}:
            raise ValueError("Этот цикл уже завершён или сброшен")
        self.transition(cycle_id, "cycle", "operator", "abandoned",
                        "Цикл сброшен оператором локально; ордера на MEXC не изменены",
                        cycle_status="abandoned")

    def cycle(self, cycle_id: str) -> dict:
        row = self.db.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ValueError(f"Цикл {cycle_id} не найден")
        return dict(row) | {"spec": json.loads(row["spec"])}

    def cycles(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, created, status FROM cycles ORDER BY created DESC")]

    def series_cycle(self, series_id: str, index: int) -> str | None:
        row = self.db.execute("""SELECT id FROM cycles WHERE json_extract(spec, '$.series.id')=?
                              AND json_extract(spec, '$.series.index')=?""", (series_id, index)).fetchone()
        return row[0] if row else None

    def step(self, cycle_id: str, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM steps WHERE cycle_id=? AND name=?", (cycle_id, name)).fetchone()
        return dict(row) | {"result": json.loads(row["result"])} if row else None

    def create_rejection_details(self, cycle_id: str, step: str) -> tuple[int, str] | None:
        """Recover the code and original time of a legacy order-create rejection."""
        if step not in {'forward_create', 'reverse_create'}:
            return None
        row = self.db.execute("""SELECT message,time FROM events WHERE cycle_id=? AND step=?
            AND status='error' AND id > COALESCE((SELECT MAX(id) FROM events WHERE cycle_id=?
            AND step=? AND status='in_flight'),0) ORDER BY id DESC LIMIT 1""",
            (cycle_id, step, cycle_id, step)).fetchone()
        marker = 'POST /api/v3/fiat/merchant/order/deal: MEXC error: '
        if not row or marker not in row['message']:
            return None
        try:
            data = json.loads(row['message'].split(marker, 1)[1])
            code = data.get('code') if isinstance(data, dict) else None
            return (code, row['time']) if code in {60085, 85010} else None
        except (ValueError, TypeError):
            return None

    def create_rejection_code(self, cycle_id: str, step: str) -> int | None:
        details = self.create_rejection_details(cycle_id, step)
        return details[0] if details else None

    def reverse_daily_limit_rejected(self, cycle_id: str) -> bool:
        return self.create_rejection_code(cycle_id, 'reverse_create') == 60085

    def forward_paid_browser_preflight_timeout(self, cycle_id: str) -> bool:
        """Recognize only the legacy read timeout before the first mark-paid call."""
        events = self.db.execute("""SELECT status, message FROM events
            WHERE cycle_id=? AND step='forward_paid' ORDER BY id""", (cycle_id,)).fetchall()
        if sum(row['status'] == 'in_flight' for row in events) != 1:
            return False
        unknown = next((index for index, row in reversed(list(enumerate(events)))
                        if row['status'] == 'unknown'), None)
        if unknown is None or 'AdsPowerTimeout' not in events[unknown]['message']:
            return False
        return any(row['status'] == 'error'
                   and 'AdsPower: команда Runtime.evaluate не ответила' in row['message']
                   for row in events[unknown + 1:])

    def transition(self, cycle_id: str, step: str, actor: str, status: str, message: str,
                   *, result: dict | None = None, context: dict | None = None,
                   cycle_status: str | None = None) -> None:
        """The step outcome and corresponding notification commit together."""
        context = context or {}
        event_time = now()
        with self.db:
            if status in {"pending", "in_flight", "unknown", "rejected", "done"}:
                self.db.execute("INSERT OR REPLACE INTO steps VALUES (?, ?, ?, ?)",
                                (cycle_id, step, status, json.dumps(result or {})))
            if cycle_status:
                self.db.execute("UPDATE cycles SET status=? WHERE id=?", (cycle_status, cycle_id))
            self.db.execute("""INSERT INTO events
                (event_id,time,cycle_id,step,actor,status,order_no,amount,fiat,quantity,message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, event_time, cycle_id, step, actor, status,
                 str(context.get("order_no", "")), str(context.get("amount", "")),
                 str(context.get("fiat", "")), str(context.get("quantity", "")), message))
            if step == "forward_complete" and status == "done":
                self.db.execute("INSERT OR IGNORE INTO sales (cycle_id, amount, quantity, completed_at) VALUES (?, ?, ?, ?)",
                                (cycle_id, str(context["amount"]), str(context.get("quantity", "")), event_time))

    def pending(self, channel: str) -> list[dict]:
        if channel != "telegram":
            raise ValueError("Invalid channel")
        return [dict(r) for r in self.db.execute(f"SELECT * FROM events WHERE {channel}_sent=0 ORDER BY id")]

    def delivered(self, channel: str, event_id: int):
        if channel != "telegram":
            raise ValueError("Invalid channel")
        with self.db:
            self.db.execute(f"UPDATE events SET {channel}_sent=1 WHERE id=?", (event_id,))

    def bind_sheet(self, target: str):
        row = self.db.execute("SELECT value FROM meta WHERE key='sheet_target'").fetchone()
        if row and row[0] != target:
            raise ValueError("Журнал привязан к другой Google Таблице/вкладке. Верните исходные настройки.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('sheet_target', ?)", (target,))

    def sheet_target(self) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key='sheet_target'").fetchone()
        return row[0] if row else None

    def pending_sales(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT sales.*,
            COALESCE(json_extract(cycles.spec, '$.p2_profile'), 'default') AS p2_profile,
            json_extract(cycles.spec, '$.nicknames.p2') AS p2_nickname,
            COALESCE(json_extract(cycles.spec, '$.p1_profile'), 'p1') AS p1_profile,
            json_extract(cycles.spec, '$.nicknames.p1') AS p1_nickname,
            json_extract(cycles.spec, '$.members.p2') AS p2_member_id,
            json_extract(cycles.spec, '$.scheduler_mode') AS scheduler_mode
            FROM sales JOIN cycles ON cycles.id=sales.cycle_id
            WHERE sales.sent=0 ORDER BY sales.id""")]

    def sales(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT sales.*,
            COALESCE(json_extract(cycles.spec, '$.p2_profile'), 'default') AS p2_profile,
            json_extract(cycles.spec, '$.nicknames.p2') AS p2_nickname,
            COALESCE(json_extract(cycles.spec, '$.p1_profile'), 'p1') AS p1_profile,
            json_extract(cycles.spec, '$.nicknames.p1') AS p1_nickname,
            json_extract(cycles.spec, '$.members.p2') AS p2_member_id,
            json_extract(cycles.spec, '$.scheduler_mode') AS scheduler_mode
            FROM sales JOIN cycles ON cycles.id=sales.cycle_id ORDER BY sales.id""")]

    def sale_delivered(self, sale_id: int):
        with self.db:
            self.db.execute("UPDATE sales SET sent=1 WHERE id=?", (sale_id,))
