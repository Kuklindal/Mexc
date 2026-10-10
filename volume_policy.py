"""Durable per-account turnover limits for the volume trading mode."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

TARGET_USDT = Decimal('70000')
CEILING_USDT = Decimal('71000')
CASH_TARGET_USDT = Decimal('68000')
CASH_CEILING_USDT = Decimal('70000')


def cash_final_return(env=None) -> str:
    """Route for the last cash-volume purchase; freeze it in each new cycle."""
    source = os.environ if env is None else env
    route = (source.get('CASH_VOLUME_FINAL_RETURN') or 'network').strip().lower()
    if route not in {'network', 'p2p'}:
        raise ValueError('CASH_VOLUME_FINAL_RETURN: укажите network или p2p')
    return route


def empty_window() -> dict:
    return {'quantity': '0', 'orders': [], 'third_order_at': None}


def window_for(state: dict, profile: str, now: datetime | None = None) -> dict:
    """Reset a profile's trading window only after 24h from its third order."""
    now = now or datetime.now(timezone.utc)
    windows = state.setdefault('volume_windows', {})
    window = windows.setdefault(profile, empty_window())
    third = window.get('third_order_at')
    if third and datetime.fromisoformat(third) + timedelta(days=1) <= now:
        window = windows[profile] = empty_window()
    return window


def record_purchase(state: dict, profile: str, cycle_id: str, quantity: str,
                    at: datetime | None = None) -> dict:
    """Count each confirmed first-leg purchase once, even after a restart."""
    at = at or datetime.now(timezone.utc)
    window = window_for(state, profile, at)
    if any(order['cycle_id'] == cycle_id for order in window['orders']):
        return window
    amount = Decimal(quantity)
    if not amount.is_finite() or amount <= 0:
        raise ValueError('Количество USDT первой покупки должно быть положительным')
    window['orders'].append({'cycle_id': cycle_id, 'at': at.isoformat(), 'quantity': str(amount)})
    window['quantity'] = str(Decimal(window['quantity']) + amount)
    if len(window['orders']) == 3:
        window['third_order_at'] = at.isoformat()
    return window


def remaining_usdt(window: dict) -> Decimal:
    return CEILING_USDT - Decimal(window['quantity'])


def should_rotate(window: dict, next_order_max_usdt: Decimal | None = None) -> bool:
    """Finish at 70k, or before an unmodified order would exceed 71k."""
    quantity = Decimal(window['quantity'])
    return (quantity >= TARGET_USDT or
            next_order_max_usdt is not None and quantity + next_order_max_usdt > CEILING_USDT)


def cooldown_until(window: dict) -> datetime:
    third = window.get('third_order_at')
    if not third:
        raise ValueError('Для таймера нужны три подтверждённые первые сделки')
    return datetime.fromisoformat(third) + timedelta(days=1)


def rolling_cash_purchases(journal, profile: str, now: datetime | None = None,
                           member_id: str | None = None) -> dict:
    """Confirmed first-leg purchases of this account in the previous 24 hours.

    The journal is authoritative across scheduler restarts and mode changes. A
    completed first leg still counts when the return leg was later abandoned.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    orders = []
    rows = journal.db.execute("""SELECT c.id, c.spec, s.result, e.time AS created_at,
                                     f.time AS completed_at
        FROM cycles c JOIN steps s ON s.cycle_id=c.id AND s.name='forward_complete'
            AND s.status='done'
        LEFT JOIN events e ON e.id=(SELECT MIN(id) FROM events
            WHERE cycle_id=c.id AND step='forward_create' AND status='done')
        LEFT JOIN events f ON f.id=(SELECT MIN(id) FROM events
            WHERE cycle_id=c.id AND step='forward_complete' AND status='done')
        WHERE f.time>=? ORDER BY f.time,c.id""", (cutoff.isoformat(),)).fetchall()
    import json
    for row in rows:
        spec = json.loads(row['spec'])
        saved_member = spec.get('members', {}).get('p2')
        if member_id and saved_member and saved_member != member_id:
            continue
        if not (member_id and saved_member) and spec.get('p2_profile', 'default') != profile:
            continue
        at = datetime.fromisoformat(row['created_at'] or row['completed_at'])
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if not cutoff < at <= now:
            continue
        quantity = Decimal(str(json.loads(row['result']).get('quantity', '')))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError(f'Нет подтверждённого количества USDT для цикла {row["id"]}')
        orders.append({'cycle_id': row['id'], 'at': at.isoformat(), 'quantity': str(quantity)})
    orders.sort(key=lambda item: item['at'])
    uncertain_until = None
    unresolved = journal.db.execute("""SELECT c.id, c.spec, c.created, e.time AS attempted_at
        FROM cycles c JOIN steps s ON s.cycle_id=c.id AND s.name='forward_create'
            AND s.status IN ('done','in_flight','unknown')
        LEFT JOIN steps completed ON completed.cycle_id=c.id
            AND completed.name='forward_complete' AND completed.status='done'
        LEFT JOIN events e ON e.id=(SELECT MIN(id) FROM events
            WHERE cycle_id=c.id AND step='forward_create' AND status IN ('in_flight','done'))
        WHERE completed.cycle_id IS NULL""").fetchall()
    for row in unresolved:
        spec = json.loads(row['spec'])
        saved_member = spec.get('members', {}).get('p2')
        if member_id and saved_member and saved_member != member_id:
            continue
        if not (member_id and saved_member) and spec.get('p2_profile', 'default') != profile:
            continue
        attempted = datetime.fromisoformat(row['attempted_at'] or row['created'])
        if attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=timezone.utc)
        if cutoff < attempted <= now:
            deadline = attempted + timedelta(days=1)
            uncertain_until = max(uncertain_until, deadline) if uncertain_until else deadline
    return {'quantity': str(sum((Decimal(item['quantity']) for item in orders), Decimal(0))),
            'orders': orders,
            'uncertain_until': uncertain_until.isoformat() if uncertain_until else None}


def rolling_cash_retry_at(window: dict) -> datetime | None:
    """Earliest time a purchase leaves the rolling window."""
    first_expiry = (min(datetime.fromisoformat(item['at']) for item in window['orders'])
                    + timedelta(days=1) if window['orders'] else None)
    uncertain_expiry = (datetime.fromisoformat(window['uncertain_until'])
                        if window.get('uncertain_until') else None)
    return max(first_expiry, uncertain_expiry) if first_expiry and uncertain_expiry else (first_expiry or uncertain_expiry)
