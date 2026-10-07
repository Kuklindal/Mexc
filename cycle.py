"""One operator-controlled, resumable two-leg USDT workflow."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from getpass import getpass
import hashlib
import json
import math
import random
import uuid
from typing import Callable

from adspower import AdsPowerClickUnknown, AdsPowerError, AdsPowerTimeout, AdsPowerUnavailable
from journal import Journal
from mexc_client import MexcAPIError, MexcChatUnavailable, MexcMutationUnknown, MexcReadUnavailable, ad_replenish_params, ad_verification, counterparty_identity
from sheets import Reporter


class AdsPowerPreflightUnavailable(AdsPowerUnavailable):
    """A browser preflight failed before the exchange action was sent."""
    pass


COMPLETED_STATES = {"DONE", "COMPLETED"}
PAID_STATES = {"PAID"} | COMPLETED_STATES
# Reverse taker SELL orders can show PROCESSING while the buyer is asked to pay.
# This permits a confirmed mark-paid request, never release or paid reconciliation.
PAYMENT_START_STATES = {"forward": {"NOT_PAID"}, "reverse": {"NOT_PAID", "PROCESSING"}}


class Paused(RuntimeError):
    pass


class CashVolumeLimitReached(Paused):
    """A cash first-order preflight rejected an order before submission."""


class OperatorStopped(Paused):
    """Requested pause at a safe boundary, not a trading failure."""


def money(value: str) -> str:
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        raise ValueError("Введите положительное число") from None
    if not number.is_finite() or number <= 0 or number > Decimal("1000000000000"):
        raise ValueError("Сумма должна быть больше нуля и не больше 1 000 000 000 000")
    return format(number, "f")


def positive_id(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("ID должен быть положительным целым числом")
    return number


def purchase_amount(value: str) -> tuple[str, str]:
    parts = value.split()
    if len(parts) not in {1, 2}:
        raise ValueError("Введите сумму и валюту, например 1000 RUB")
    fiat = parts[1].upper() if len(parts) == 2 else "RUB"
    if len(fiat) != 3 or not fiat.isascii() or not fiat.isalpha():
        raise ValueError("Валюта — три латинские буквы, например RUB или KZT. Пример ввода: 1000 RUB")
    return money(parts[0]), fiat


class Console:
    def __init__(self, read: Callable = input, write: Callable = print):
        self.read = read
        self.write = write

    def ask(self, prompt: str, default: str = "", validate: Callable | None = None) -> str:
        while True:
            try:
                raw = self.read(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
            except EOFError:
                raise OperatorStopped("Ввод закрыт; прогресс сохранён") from None
            if raw.lower() in {"stop", "стоп", "q"}:
                raise OperatorStopped("Остановлено оператором; прогресс сохранён")
            value = raw or default
            if not value:
                self.write("Пустое значение не принято. Для паузы введите STOP.")
                continue
            try:
                return validate(value) if validate else value
            except (ValueError, InvalidOperation) as exc:
                self.write(str(exc))

    def confirm(self, prompt: str, phrase: str = "ДА"):
        self.write(prompt)
        if self.ask(f"Для подтверждения введите {phrase}; иначе STOP") != phrase:
            raise OperatorStopped("Подтверждение не получено; прогресс сохранён")


class AutoConsole(Console):
    """No stdin in automatic runs. Uncertain recovery still needs an operator."""
    def ask(self, prompt: str, default: str = "", validate: Callable | None = None):
        raise Paused(f"Авторежиму не хватает настройки: {prompt}. Продолжите с --interactive.")

    def confirm(self, prompt: str, phrase: str = "ДА"):
        if phrase.startswith(("ПОВТОРИТЬ", "ОРДЕР НЕ СОЗДАН")):
            raise Paused("Повтор операции требует сверки оператором. Продолжите с --interactive.")
        self.write("Авто: " + prompt)


def action_delay(value: str) -> float:
    delay = float(value)
    if not 0 <= delay <= 3600:
        raise ValueError("Пауза между действиями должна быть от 0 до 3600 секунд")
    return delay


def delay_bounds(env: dict) -> tuple[float, float]:
    low, high = env.get("ACTION_DELAY_MIN_SECONDS", ""), env.get("ACTION_DELAY_MAX_SECONDS", "")
    if not low and not high:
        low = high = env.get("ACTION_DELAY_SECONDS", "20")
    if not low or not high:
        raise ValueError("Заполните обе границы ACTION_DELAY_MIN_SECONDS и ACTION_DELAY_MAX_SECONDS")
    low, high = action_delay(low), action_delay(high)
    if low > high:
        raise ValueError("Минимальная пауза не может быть больше максимальной")
    return low, high


MUTATING_ACTIONS = {"create", "message", "reply", "check", "paid", "release", "replenish"}


@dataclass(frozen=True)
class Step:
    key: str
    actor: str
    label: str


STEPS = [
    Step("forward_ad", "p1", "Подтверждение готового объявления П1 о продаже USDT"),
    Step("forward_create", "p2", "П2 открывает покупку USDT у П1"),
    Step("forward_verify", "both", "Сверка первой сделки и участников"),
    Step("forward_message", "p2", "П2 пишет продавцу"),
    Step("forward_reply", "p1", "П1 отвечает покупателю"),
    Step("forward_check", "p1", "П1 нажимает «Проверка пройдена» на MEXC"),
    Step("forward_paid", "p2", "П2 оплачивает сделку и отмечает оплату"),
    Step("forward_release", "p1", "П1 проверяет получение денег и выпускает USDT"),
    Step("forward_complete", "both", "Первая продажа завершена, USDT получены П2"),
    Step("reverse_ad", "p1", "Выбор готового объявления П1 о покупке USDT"),
    Step("reverse_create", "p2", "П2 продаёт полученные USDT в объявление П1"),
    Step("reverse_verify", "both", "Сверка обратной сделки и участников"),
    Step("reverse_message", "p2", "П2 пишет покупателю"),
    Step("reverse_reply", "p1", "П1 отвечает продавцу"),
    Step("reverse_paid", "p1", "П1 оплачивает сделку и отмечает оплату"),
    Step("reverse_release", "p2", "П2 проверяет получение денег и выпускает USDT"),
    Step("reverse_complete", "both", "Обратная сделка завершена, USDT получены П1"),
    Step("reverse_replenish_buy", "p1", "П1 пополняет объявление покупки USDT после обратной сделки"),
    Step("reverse_replenish", "p1", "П1 пополняет исходное объявление продажи полученными USDT"),
]


def fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def steps_for_spec(spec: dict) -> list[Step]:
    if spec.get('scheduler_mode') == 'cash_volume' and spec.get('cash_return_route') == 'network':
        # The scheduler performs the saved, reconciled wallet return after the
        # confirmed first leg. No reverse P2P order is created on this route.
        return STEPS[:9]
    if spec.get('reverse_maker', 'p1') != 'p2':
        if spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}:
            result = []
            for step in STEPS:
                if step.key == 'forward_check':
                    continue  # These order flows go straight to payment.
                if step.key in {'forward_paid', 'reverse_paid'}:
                    result.append(Step(step.key.replace('_paid', '_wait_paid'), 'both',
                                       'Ожидание перед отметкой оплаты'))
                if step.key in {'forward_release', 'reverse_release'}:
                    result.append(Step(step.key.replace('_release', '_wait_release'), 'both',
                                       'Ожидание перед подтверждением получения денег'))
                result.append(step)
            return result
        return STEPS
    steps = STEPS[:9] + [
        Step('reverse_replenish_sell', 'p2', 'П2 пополняет своё объявление продажи USDT'),
        Step('reverse_ad', 'p2', 'Проверка объявления продажи USDT у П2'),
        Step('reverse_create', 'p1', 'П1 покупает USDT по объявлению П2'),
        Step('reverse_verify', 'both', 'Сверка обратной сделки и участников'),
        Step('reverse_message', 'p1', 'П1 пишет продавцу'),
        Step('reverse_reply', 'p2', 'П2 отвечает покупателю'),
    ]
    steps.append(Step('reverse_check', 'p2', 'П2 проверяет документы покупателя'))
    if spec.get('scheduler_mode') == 'cash_volume':
        steps.append(Step('reverse_wait_paid', 'both', 'Ожидание перед оплатой обратного ордера'))
    steps.append(Step('reverse_paid', 'p1', 'П1 оплачивает сделку и отмечает оплату'))
    if spec.get('scheduler_mode') == 'cash_volume':
        steps.append(Step('reverse_wait_release', 'both', 'Ожидание перед подтверждением получения денег'))
    steps += [
        Step('reverse_release', 'p2', 'П2 проверяет получение денег и выпускает USDT'),
        Step('reverse_complete', 'both', 'Обратная сделка завершена, USDT получены П1'),
        Step('reverse_replenish', 'p1', 'П1 пополняет исходное объявление продажи полученными USDT'),
    ]
    return steps


def new_spec(console: Console, mode: str, profiles: dict[str, str], *,
             sell_adv_no: str = "", buy_adv_no: str = "", amount_input: str = "", automatic: bool = False,
             reverse_maker: str = 'p1', p1_profile: str = 'p1') -> dict:
    amount, fiat = purchase_amount(amount_input) if amount_input else console.ask(
        "Сумма покупки П2 (например 1000 RUB; без валюты — RUB)", validate=purchase_amount)
    console.write(f"П2 купит у П1 USDT на {amount} {fiat}.")
    adv_no = sell_adv_no.strip() or console.ask("Номер готового объявления П1 о продаже USDT (advNo)")
    return {"mode": mode, "amount": amount, "fiat": fiat, "profiles": profiles,
            "forward_adv_no": adv_no, "reverse_adv_no": buy_adv_no.strip(), "automatic": automatic,
            "buy_replenish": reverse_maker == 'p1', 'reverse_maker': reverse_maker,
            'p1_profile': p1_profile}


def auto_plan(args, env: dict) -> dict:
    """Validate the whole batch before creating its first cycle."""
    count = args.count if args.count is not None else 1
    if count < 1:
        raise ValueError("Количество циклов должно быть положительным целым числом")
    explicit_range = args.min_amount is not None or args.max_amount is not None
    if args.amount:
        if explicit_range or args.fiat:
            raise ValueError("Укажите либо --amount, либо --min-amount / --max-amount / --fiat")
        low, fiat = purchase_amount(args.amount)
        high = low
    else:
        low, high = ((args.min_amount, args.max_amount) if explicit_range else
                     (env.get("AUTO_MIN_AMOUNT"), env.get("AUTO_MAX_AMOUNT")))
        if not low or not high:
            raise ValueError("Укажите --amount или обе границы --min-amount и --max-amount (либо AUTO_MIN_AMOUNT / AUTO_MAX_AMOUNT в .env)")
        low, high = money(low), money(high)
        if any(Decimal(v) * 100 != (Decimal(v) * 100).to_integral_value() for v in (low, high)):
            raise ValueError("Границы суммы могут содержать не больше двух знаков после запятой")
        _, fiat = purchase_amount("1 " + (args.fiat or env.get("AUTO_FIAT", "RUB")))
    if Decimal(low) > Decimal(high):
        raise ValueError("Минимальная сумма не может быть больше максимальной")
    return {"id": uuid.uuid4().hex, "index": 1, "count": count,
            "min_amount": low, "max_amount": high, "fiat": fiat}


def random_amount(plan: dict) -> str:
    low, high = Decimal(plan["min_amount"]), Decimal(plan["max_amount"])
    if low == high:
        return str(low)
    return format(Decimal(random.randint(int(low * 100), int(high * 100))) / 100, ".2f")


async def run_series(runner, cycle_id: str):
    """Persist each chosen amount before running it; resume follows saved successors."""
    while True:
        runner.check_stop()
        spec = runner.journal.cycle(cycle_id)["spec"]
        series = spec.get("series")
        if series:
            runner.console.write(f"Серия: цикл {series['index']} из {series['count']}; "
                                 f"первая покупка {spec['amount']} {spec['fiat']}. "
                                 f"Продолжение: cycle --resume {cycle_id}")
        await runner.run(cycle_id)
        if not runner.automatic or not series or series["index"] >= series["count"]:
            if (runner.automatic and series and series["index"] == series["count"]
                    and runner.journal.cycle(cycle_id)['status'] == 'completed'):
                runner.console.write(f"Серия завершена: {series['count']} циклов.")
            return
        if (runner.journal.cycle(cycle_id)["status"] != "completed"
                or not runner.result("reverse_replenish")
                or (spec.get("buy_replenish") and not runner.result("reverse_replenish_buy"))):
            raise Paused("Следующий цикл не запущен: предыдущий цикл и пополнение не завершены")
        following = dict(series, index=series["index"] + 1)
        runner.check_stop()
        next_id = runner.journal.series_cycle(series["id"], following["index"])
        if next_id is None:
            next_id = runner.journal.create(dict(spec, amount=random_amount(series), series=following))
        cycle_id = next_id


class CycleRunner:
    def __init__(self, journal: Journal, reporter: Reporter, clients: dict,
                 console: Console, *, state_changes: bool, p2_payment_id: str = "", browser=None,
                 p2_pay_method_id: str = "", p2_payment_browser=None,
                 p2_payment_account_id: int | None = None,
                 pay_method_id: str = "", automatic: bool = False, delay_seconds: float = 20,
                 trusted_members: dict | None = None, p1_over_verify: str = "",
                 trusted_nicknames: dict | None = None, delay_max_seconds: float | None = None,
                 p2_profile: str = "default", stop_event: asyncio.Event | None = None,
                 p1_profile: str = 'p1', maker_browser=None):
        self.journal = journal
        self.reporter = reporter
        self.clients = clients
        self.console = console
        self.state_changes = state_changes
        self.p2_payment_id = p2_payment_id.strip()
        self.p2_pay_method_id = positive_id(p2_pay_method_id) if p2_pay_method_id else None
        self.p2_payment_browser = p2_payment_browser
        self.p2_payment_account_id = p2_payment_account_id
        self.p2_profile = p2_profile
        self.browser = browser
        self.maker_browser = maker_browser
        self.p1_profile = p1_profile
        self.pay_method_id = positive_id(pay_method_id) if pay_method_id else None
        self.automatic = automatic
        self.delay_seconds = action_delay(str(delay_seconds))
        self.delay_max_seconds = action_delay(str(delay_seconds if delay_max_seconds is None else delay_max_seconds))
        if self.delay_seconds > self.delay_max_seconds:
            raise ValueError("Минимальная пауза не может быть больше максимальной")
        self.trusted_members = trusted_members or {}
        self.trusted_nicknames = trusted_nicknames or {}
        self.p1_over_verify = ad_verification({}, p1_over_verify) if p1_over_verify else ""
        self.cycle_id = ""
        self.spec: dict = {}
        self._last_reported: dict = {}
        self.stop_event = stop_event

    def steps(self) -> list[Step]:
        return steps_for_spec(self.spec)

    def browser_for_step(self, step: Step):
        return self.maker_browser if step.key == 'reverse_check' else self.browser

    def check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise OperatorStopped("Остановлено оператором; прогресс сохранён, ордера MEXC не отменены")

    async def wait_delay(self, seconds: float):
        if self.stop_event is None:
            await asyncio.sleep(seconds)
        else:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass
            self.check_stop()

    def result(self, key: str) -> dict:
        step = self.journal.step(self.cycle_id, key)
        return step["result"] if step and step["status"] == "done" else {}

    def context(self, key: str) -> dict:
        leg = key.split("_")[0]
        detail = self.result(f"{leg}_verify")
        order = self.result(f"{leg}_create")
        return {"amount": detail.get("amount", self.spec["amount"] if leg == "forward" else ""),
                "fiat": self.spec["fiat"], "quantity": detail.get("quantity", ""),
                 "order_no": order.get("order_no", "")}

    def select_cash_reverse_route(self) -> bool:
        """Freeze the return route after the exchange confirms the first sale."""
        if self.spec.get('scheduler_mode') != 'cash_volume' or self.spec.get('cash_route_selected'):
            return False
        sale = self.result('forward_complete')
        if not sale:
            raise Paused('Нельзя выбрать обратный маршрут до подтверждения первой продажи')
        from rollover import load_state, save_state
        from volume_policy import (TARGET_USDT, CEILING_USDT, CASH_CEILING_USDT,
                                   record_purchase, rolling_cash_purchases)
        scheduler = load_state(self.journal)
        if (not scheduler or scheduler.get('mode') != 'cash_volume'
                or self.p2_profile not in scheduler.get('profiles', [])
                or scheduler.get('p1_profile') != self.p1_profile):
            raise Paused('Режим и участники сохранённой серии изменились; обратный маршрут не выбран')
        row = self.journal.db.execute(
            "SELECT time FROM events WHERE cycle_id=? AND step='forward_create' "
            "AND status='done' ORDER BY id LIMIT 1", (self.cycle_id,)).fetchone()
        at = datetime.fromisoformat(row['time']) if row else datetime.now(timezone.utc)
        if self.spec.get('cash_policy') == 'rolling24_p1':
            window = rolling_cash_purchases(self.journal, self.p2_profile,
                                            member_id=self.trusted_members.get('p2'))
            spec = dict(self.spec, cash_route_selected=True,
                        cash_volume_at_route=window['quantity'],
                        reverse_maker='p1', buy_replenish=True,
                        cash_return_route='ordinary',
                        reverse_adv_no=self.spec['cash_p1_buy_adv_no'])
            with self.journal.db:
                self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?',
                                        (json.dumps(spec), self.cycle_id))
            self.spec = spec
            over_limit = Decimal(window['quantity']) > CASH_CEILING_USDT
            self.journal.transition(self.cycle_id, 'cash_route', 'system', 'done',
                ('⚠️ Скользящий объём превысил 70 000 USDT; обратную сделку завершить, '
                 'новые покупки П2 заблокированы' if over_limit else
                 'Обычный обратный ордер: П2 продаёт USDT в объявление покупки П1'),
                result={'reverse_maker': 'p1', 'return_route': 'ordinary',
                        'volume_usdt': window['quantity'], 'limit_breached': over_limit})
            return True
        window = record_purchase(scheduler, self.p2_profile, self.cycle_id, sale['quantity'], at)
        save_state(self.journal, scheduler)
        # Finish within the agreed 70–71k band, before the next ordinary
        # order of this size could cross the hard ceiling.
        total = Decimal(window['quantity'])
        final = total >= TARGET_USDT or total + Decimal(sale['quantity']) > CEILING_USDT
        network = final and self.spec.get('cash_final_return', 'p2p') == 'network'
        spec = dict(self.spec, cash_route_selected=True,
                     cash_volume_at_route=window['quantity'],
                     cash_third_order_at=window.get('third_order_at'),
                     reverse_maker='p2' if final and not network else 'p1',
                     buy_replenish=not final,
                     cash_return_route='network' if network else 'p2p' if final else 'ordinary',
                     reverse_adv_no=(self.spec['cash_p2_sell_adv_no'] if final
                                     and not network else self.spec['cash_p1_buy_adv_no']))
        with self.journal.db:
            self.journal.db.execute('UPDATE cycles SET spec=? WHERE id=?',
                                    (json.dumps(spec), self.cycle_id))
        self.spec = spec
        self.journal.transition(self.cycle_id, 'cash_route', 'system', 'done',
            ('Достигнут порог объёма; последний остаток вернётся через сеть после сверки вывода'
             if network else 'Достигнут порог объёма; П1 выкупит последний ордер через объявление П2'
             if final else 'Обычный цикл: П2 продаст USDT в объявление покупки П1'),
            result={'reverse_maker': spec['reverse_maker'], 'return_route': spec['cash_return_route'],
                    'volume_usdt': window['quantity']})
        return True

    async def event(self, step: Step, status: str, message: str, result: dict | None = None,
                    cycle_status: str | None = None):
        context = self.context(step.key) | {k: v for k, v in (result or {}).items()
                                            if k in {"amount", "fiat", "quantity", "order_no"}}
        self.journal.transition(self.cycle_id, step.key, step.actor, status, message,
                                result=result, context=context, cycle_status=cycle_status)
        await self.reporter.flush()

    async def run(self, cycle_id: str):
        cycle = self.journal.cycle(cycle_id)
        self.cycle_id, self.spec = cycle_id, cycle["spec"]
        self._last_reported.clear()
        if self.spec.get("p2_profile", "default") != self.p2_profile:
            raise ValueError("Профиль П2 не совпадает с сохранённым циклом")
        if self.spec.get('p1_profile', 'p1') != self.p1_profile:
            raise ValueError('Профиль П1 не совпадает с сохранённым циклом')
        if "p2_payment_id" in self.spec and self.spec["p2_payment_id"] != self.p2_payment_id:
            raise ValueError("Реквизиты П2 изменились; верните настройки начатого цикла")
        if self.spec.get('p2_pay_method_id') and int(self.spec['p2_pay_method_id']) != self.p2_pay_method_id:
            raise ValueError('Способ оплаты П2 изменился; верните настройки начатого цикла')
        if self.spec.get("members") and self.spec["members"] != self.trusted_members:
            raise ValueError("Разрешённые участники изменились; верните настройки начатого цикла")
        if self.spec.get("nicknames") and self.spec["nicknames"] != self.trusted_nicknames:
            raise ValueError("Разрешённые ники изменились; верните настройки начатого цикла")
        if (self.automatic or self.trusted_members) and (
                set(self.trusted_members) != {"p1", "p2"} or not all(self.trusted_members.values())
                or self.trusted_members["p1"] == self.trusted_members["p2"]):
            raise ValueError("Для авторежима нужны MEXC_P1_MEMBER_ID и MEXC_P2_MEMBER_ID")
        if (self.automatic or self.trusted_members) and not self.trusted_nicknames.get("p2"):
            raise ValueError("Для проверки П2 нужен точный MEXC_P2_NICKNAME")
        if cycle["status"] == "abandoned":
            raise ValueError("Этот цикл сброшен. Для нового цикла используйте команду cycle.")
        if self.spec.get('cash_return_route') == 'network' and self.result('forward_complete'):
            returned = self.journal.step(cycle_id, 'cash_network_return')
            if cycle['status'] == 'completed' and returned and returned['status'] == 'done':
                self.console.write('Вывод через сеть уже завершён. Повторных операций не будет.')
                return
            raise Paused('Последняя покупка подтверждена; вывод через сеть выполняет сохранённая серия. '
                         'Продолжите серию через Telegram, не повторяйте цикл отдельно.')
        if (cycle["status"] == "completed" and self.result("reverse_replenish")
                and (not self.spec.get("buy_replenish") or self.result("reverse_replenish_buy"))):
            self.console.write("Этот цикл уже завершён. Повторных операций не будет.")
            await self.reporter.flush()
            await self.close_completed_tabs()
            return
        self.console.write(f"Цикл {cycle_id}. STOP — пауза. Возобновление: cycle --resume {cycle_id}")
        self.console.write(f"П2: профиль {self.p2_profile}; ник {self.trusted_nicknames.get('p2', 'не задан')}.")
        self.console.write("Авторежим: действия без запросов в консоли; Ctrl+C — остановка. Фактический перевод денег не проверяется."
                           if self.automatic else "Режим: подтверждения в консоли, действия через API и AdsPower."
                           if self.spec["mode"] == "api" and self.browser else
                           "Режим: API с ручной проверкой продавца на сайте." if self.spec["mode"] == "api" else
                           "Режим: действия выполняются вручную на сайте MEXC.")
        if self.spec["mode"] == "api" and not self.state_changes:
            raise ValueError("Для API-цикла установите ENABLE_STATE_CHANGES=true; для ручного используйте --mode manual")
        current = Step("cycle", "both", "Цикл")
        try:
            if self.automatic:
                await self.guard_participants()
            for step in self.steps():
                current = step
                self.check_stop()
                if step.key in {'forward_wait_paid', 'forward_wait_release',
                                'reverse_wait_paid', 'reverse_wait_release'}:
                    saved_wait = self.journal.step(cycle_id, step.key)
                    if saved_wait and saved_wait['status'] == 'done':
                        continue
                    if saved_wait and saved_wait['status'] == 'pending' and saved_wait['result'].get('until'):
                        until = datetime.fromisoformat(saved_wait['result']['until'])
                    else:
                        prefix = 'eflp' if self.spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'} else 'cash'
                        lower = float(self.spec.get(prefix + '_wait_min_seconds', 75))
                        upper = float(self.spec.get(prefix + '_wait_max_seconds', 105))
                        until = datetime.now(timezone.utc) + timedelta(seconds=random.uniform(lower, upper))
                        await self.event(step, 'pending', step.label, {'until': until.isoformat()})
                    while (remaining := (until - datetime.now(timezone.utc)).total_seconds()) > 0:
                        await self.wait_delay(min(5, remaining))
                    await self.event(step, 'done', step.label + ': время истекло', {'until': until.isoformat()})
                    continue
                if step.key == "reverse_replenish_buy" and not self.spec.get("buy_replenish"):
                    continue
                saved = self.journal.step(cycle_id, step.key)
                legacy_rejection = (self.journal.create_rejection_code(cycle_id, step.key)
                                    if saved and saved['status'] == 'unknown'
                                    and not saved['result'].get('order_no') else None)
                if legacy_rejection:
                    self.journal.transition(cycle_id, step.key, step.actor, 'rejected',
                        f'Восстановлен явный отказ MEXC {legacy_rejection} из журнала последней попытки',
                        result={'rejected_code': legacy_rejection}, context=self.context(step.key))
                    saved = self.journal.step(cycle_id, step.key)
                if (saved and saved['status'] == 'rejected'
                        and saved['result'].get('rejected_code') in {60085, 85010}):
                    rejection_message = (self.daily_limit_message() if saved['result']['rejected_code'] == 60085
                                         else self.ad_rejection_message())
                    if self.automatic:
                        raise Paused(rejection_message)
                    self.console.confirm(
                        rejection_message + "\nПодтвердите, что MEXC теперь разрешает эту сделку.",
                        f"ЛИМИТ ПРОВЕРЕН {step.key}")
                    saved = None
                if saved and saved['status'] == 'rejected' and saved['result'].get('rejected_code') == 700003:
                    # MEXC explicitly rejected the timestamp before processing.
                    # A later resume may safely create a fresh signed request.
                    saved = None
                leg, action = step.key.split("_", 1)
                if step.key in {"reverse_replenish_buy", "reverse_replenish_sell"}:
                    action = "replenish"
                if (self.automatic and action == 'create' and saved
                        and saved['status'] in {'in_flight', 'unknown'}):
                    if await self.reconcile_unknown_create(step, saved['result']):
                        continue
                    saved = None
                remote = None
                check_browser = self.browser_for_step(step) if action == 'check' else None
                if (action == 'check' and saved and saved["status"] == "done"
                        and check_browser and self.spec["mode"] == "api"
                        and not self.result(f'{leg}_paid')):
                    fresh = await self.snapshot(leg)
                    verification = None
                    if fresh["state"] == "NOT_PAID":
                        try:
                            verification = await check_browser.inspect(fresh["order_no"])
                        except (AdsPowerTimeout, AdsPowerUnavailable) as exc:
                            raise AdsPowerPreflightUnavailable(str(exc)) from exc
                    if verification is not None and verification not in {"passed", "not_required"}:
                        await self.event(step, "pending", "Проверка документов не подтверждена; прежняя отметка по заголовку отменена")
                        saved = None
                if self.spec["mode"] == "api" and action in {"paid", "release"}:
                    remote = await self.snapshot(leg)
                    if (self.automatic and step.key == "forward_paid" and saved
                            and saved["status"] == "unknown"
                            and remote["state"] in PAYMENT_START_STATES[leg]
                            and not self.result("forward_release")
                            and self.journal.forward_paid_browser_preflight_timeout(cycle_id)):
                        # Recover a cycle stopped by the old handler: the only
                        # AdsPower call in forward_paid precedes mark_paid.
                        await self.event(step, "pending",
                                         "Повтор чтения AdsPower: запрос отметки оплаты не отправлялся",
                                         saved["result"])
                        saved = None
                    if saved and saved["status"] == "done":
                        expected = PAID_STATES if action == "paid" else COMPLETED_STATES
                        if remote["state"] not in expected:
                            if action == "paid" and remote["state"] in PAYMENT_START_STATES[leg] and not self.result(f"{leg}_release"):
                                # Repair a legacy local acknowledgement contradicted by the exchange.
                                await self.event(step, "pending", f"MEXC всё ещё {remote['state']}: местная отметка отменена; требуется подтверждение запроса")
                                saved = None
                            else:
                                raise Paused(f"Состояние MEXC {remote['state']} противоречит сохранённому шагу {step.key}. Проверьте ордер на бирже.")
                if saved and saved["status"] == "done":
                    if step.key == 'forward_complete' and self.select_cash_reverse_route():
                        return
                    continue
                await self.event(step, "waiting", step.label, cycle_status="active" if action == "replenish" else None)
                self.console.write(f"\n[{step.key}] {step.label}")
                uncertain = bool(saved and saved["status"] in {"in_flight", "unknown"})
                retry = False
                chat_retry = self.automatic and uncertain and action in {"message", "reply"}
                if chat_retry:
                    previous = saved["result"]
                    if (not isinstance(previous.get("text"), str) or not previous["text"]
                            or previous.get("order_no") != self.result(f"{leg}_create")["order_no"]):
                        raise Paused("Нельзя автоматически повторить сообщение: в журнале нет точного текста и ордера")
                    # The operator allows duplicate chat text. Keep the saved
                    # phrase, but never apply this exception to trade actions.
                    retry, uncertain = True, False
                already_applied = remote is not None and remote["state"] in (PAID_STATES if action == "paid" else COMPLETED_STATES)
                if already_applied:
                    uncertain = True
                if self.automatic and uncertain and action == 'check':
                    fresh = await self.snapshot(leg)
                    self.check_state(fresh, {'NOT_PAID'})
                    order_no = fresh['order_no']
                    state = await check_browser.open_order(order_no)
                    if state in {'passed', 'not_required'}:
                        await self.event(step, 'done', 'Проверка документов подтверждена MEXC',
                                         {'order_no': order_no, 'verification': state})
                        continue
                    if state != 'ready':
                        raise Paused('Проверка документов не подтверждена; состояние кнопки неизвестно')
                    self.check_stop()
                    await self.guard_participants()
                    await self.event(step, 'in_flight', 'Повтор нажатия после сверки состояния кнопки',
                                     {'order_no': order_no, 'verification': 'adspower'})
                    try:
                        await check_browser.approve(order_no)
                    except BaseException as exc:
                        self.journal.transition(cycle_id, step.key, step.actor, 'unknown',
                            f'Результат нажатия требует сверки ({type(exc).__name__})',
                            context=self.context(step.key), result={'order_no': order_no})
                        raise
                    await self.event(step, 'done', 'Проверка документов пройдена на MEXC',
                                     {'order_no': order_no, 'verification': 'seller_check_completed_on_mexc'})
                    continue
                if self.automatic and uncertain and action == 'replenish':
                    # A timed-out update may already have reached MEXC. Reconcile
                    # against the saved absolute target; never add the increment twice.
                    plan = saved['result']
                    if plan.get('adv_no') and plan.get('target_available'):
                        ad = await self.clients[step.actor].get_ad(plan['adv_no'])
                        try:
                            self.check_replenished(ad, plan)
                        except Paused:
                            browser = self.maker_browser if step.actor == 'p2' else self.browser
                            current_plan = (await self.quantity_plan(ad, plan['adv_no'], plan['quantity'],
                                        side=plan.get('side', 'SELL'), browser=browser)
                                       if plan.get('method') == 'quantity_only' else
                                       self.replenish_plan(ad, plan['adv_no'], plan['quantity']))
                            if current_plan != plan:
                                raise Paused('Остаток или параметры объявления изменились; автоматический повтор пополнения остановлен')
                            retry, uncertain = True, False
                        else:
                            if plan.get('method') == 'quantity_only':
                                await self.check_browser_ad(plan, browser=self.maker_browser if step.actor == 'p2' else self.browser)
                            await self.event(step, 'done', 'Пополнение подтверждено текущим остатком MEXC', plan)
                            continue
                if self.automatic and uncertain and remote is not None and not already_applied:
                    self.check_state(remote, PAYMENT_START_STATES[leg] if action == 'paid' else {'PAID'})
                    retry, uncertain = True, False
                if self.automatic and uncertain and not already_applied:
                    raise Paused("Результат предыдущей операции требует сверки. Автоматического повтора нет; продолжите с --interactive.")
                if uncertain and action == "replenish" and saved["result"].get("rejected_code") in {700002, 60048, 60064}:
                    plan = {k: v for k, v in saved["result"].items() if k != "rejected_code"}
                    ad = await self.clients[step.actor].get_ad(plan["adv_no"])
                    if Decimal(str(ad["availableQuantity"])) != Decimal(plan["target_available"]):
                        if self.replenish_plan(ad, plan["adv_no"], plan["quantity"]) != plan:
                            raise Paused("После отказа MEXC объявление изменилось. Повтор пополнения остановлен; требуется сверка.")
                        retry, uncertain = True, False
                        self.console.write(f"Предыдущий запрос пополнения отклонён MEXC ({saved['result']['rejected_code']}). "
                                           "Остаток и параметры объявления не изменились. Повтор требует подтверждения ниже.")
                if uncertain and action == "create" and self.spec["mode"] == "api":
                    self.console.write("Проверьте историю ордеров П1 и П2 на MEXC, включая завершённые и отменённые.\n"
                                       "Если сделка создана, укажите её номер далее. Если после отказа API ордера нет, можно повторить создание.")
                    def creation_result(value: str) -> str:
                        if value not in {"СОЗДАН", "НЕ СОЗДАН"}:
                            raise ValueError("Введите СОЗДАН или НЕ СОЗДАН; если не уверены — STOP")
                        return value
                    if self.console.ask("Результат предыдущей попытки", "СОЗДАН", validate=creation_result) == "НЕ СОЗДАН":
                        self.console.confirm("Подтвердите: история обоих аккаунтов проверена, ордер предыдущей попытки отсутствует.",
                                             f"ОРДЕР НЕ СОЗДАН {step.key}")
                        retry, uncertain = True, False
                if uncertain and remote is not None:
                    expected = PAID_STATES if action == "paid" else COMPLETED_STATES
                    if remote["state"] not in expected:
                        before = PAYMENT_START_STATES[leg] if action == "paid" else {"PAID"}
                        self.check_state(remote, before)
                        retry, uncertain = True, False
                        self.console.write(f"MEXC: {remote['state']}. Шаг {step.key} не подтверждён биржей.\n"
                                           "Повтор API-запроса возможен только после отдельного подтверждения ниже. "
                                           "Повторно платить деньги не нужно.")
                if uncertain:
                    self.console.write("MEXC подтверждает результат этого шага. Повторного API-запроса не будет."
                                       if already_applied else
                                       "Предыдущая попытка имеет неизвестный результат. Повторного API-запроса не будет.\n"
                                       "Проверьте MEXC и при необходимости завершите ЭТОТ шаг вручную. "
                                       "Если завершить нельзя, введите STOP.")
                paced = (action in {'paid', 'release'}
                         and (wait_step := self.journal.step(cycle_id, f'{leg}_wait_{action}'))
                         and wait_step['status'] == 'done')
                if self.automatic and action in MUTATING_ACTIONS and not uncertain and not paced:
                    remaining = random.uniform(self.delay_seconds, self.delay_max_seconds)
                    self.console.write(f"Пауза {remaining:.2f} сек. перед действием.")
                    while remaining > 0:
                        await self.wait_delay(min(5, remaining))
                        remaining -= min(5, remaining)
                        await self.guard_participants()
                self.check_stop()
                prepared = dict(saved["result"]) if chat_retry else await self.prepare(step, recovery=uncertain)
                if uncertain:
                    self.console.confirm("Подтверждаю, что результат этого шага проверен на MEXC.", f"СВЕРЕНО {step.key}")
                else:
                    self.console.confirm(step.label + "\n" + json.dumps(
                        {k: v for k, v in prepared.items() if k not in {"notify_code", "settings_fingerprint"}}, ensure_ascii=False, indent=2),
                        f"ПОВТОРИТЬ {step.key}" if retry and not self.automatic else "ДА")
                # Durable intent is committed before any mutating network request.
                self.check_stop()
                payment_context = ({"payment_account_id": prepared["payment_account_id"]}
                                   if "payment_account_id" in prepared else None)
                if action in {"message", "reply"}:
                    # Preserve the exact phrase before the network call, so an
                    # uncertain send can be reconciled without changing text.
                    payment_context = {"order_no": prepared["order_no"], "text": prepared["text"]}
                if action == 'create':
                    payment_context = dict(prepared)
                if action == 'check':
                    payment_context = {'order_no': prepared['order_no'],
                                       'verification': prepared.get('verification')}
                if action == "replenish":
                    payment_context = prepared
                await self.event(step, "in_flight", step.label + (": авторежим" if self.automatic else ": подтверждено оператором"), payment_context)
                try:
                    result = await self.execute(step, prepared, recovery=uncertain)
                except BaseException as exc:
                    if isinstance(exc, AdsPowerPreflightUnavailable):
                        self.journal.transition(cycle_id, step.key, step.actor, 'pending',
                            'AdsPower недоступен до отправки действия на MEXC; шаг можно повторить',
                            context=self.context(step.key), result=payment_context)
                        raise
                    if (isinstance(exc, MexcAPIError) and exc.code == 700003
                            and exc.http_status == 400):
                        self.journal.transition(cycle_id, step.key, step.actor, 'rejected',
                            'MEXC отклонил время подписи; запрос не выполнен',
                            result={k: v for k, v in prepared.items() if k != 'notify_code'}
                                   | {'rejected_code': 700003}, context=self.context(step.key))
                        raise
                    if (action == 'create' and isinstance(exc, MexcAPIError)
                            and exc.code in {60085, 85010} and exc.http_status in {200, 400}):
                        self.journal.transition(cycle_id, step.key, step.actor, 'rejected',
                            f'Создание ордера отклонено MEXC: код {exc.code}',
                            result=dict(prepared, rejected_code=exc.code), context=self.context(step.key))
                        raise Paused(self.daily_limit_message() if exc.code == 60085
                                     else self.ad_rejection_message()) from exc
                    # Do not log exception URLs, which may contain API signatures or listen keys.
                    if (action == "replenish" and isinstance(exc, MexcAPIError)
                            and ((exc.code == 700002 and exc.http_status == 400)
                                 or (exc.code in {60048, 60064} and exc.http_status in {200, 400}))):
                        payment_context = dict(payment_context or {}) | {"rejected_code": exc.code}
                    self.journal.transition(cycle_id, step.key, step.actor, "unknown",
                        f"Результат требует сверки на MEXC ({type(exc).__name__})",
                        context=self.context(step.key), result=payment_context)
                    raise
                await self.event(step, "done", step.label + (" (сверено вручную)" if uncertain else ""), result)
                if step.key == 'forward_complete' and self.select_cash_reverse_route():
                    return
            await self.event(Step("cycle", "both", "Цикл"), "completed", "Цикл завершён", cycle_status="completed")
            await self.close_completed_tabs()
            self.console.write("Цикл завершён.")
        except BaseException as exc:
            transient_adspower = self.automatic and isinstance(exc, (AdsPowerUnavailable, AdsPowerTimeout))
            transient_mexc_read = self.automatic and isinstance(exc, MexcReadUnavailable)
            transient_chat = self.automatic and isinstance(exc, MexcChatUnavailable)
            transient_mutation = (self.automatic and isinstance(exc, MexcMutationUnknown)
                                  and (current.key.split('_', 1)[-1] in
                                       {'create', 'message', 'reply', 'paid', 'release', 'replenish'}
                                       or current.key in {'reverse_replenish_buy', 'reverse_replenish_sell'}))
            transient_click = (self.automatic and isinstance(exc, AdsPowerClickUnknown)
                               and (current.key.split('_', 1)[-1] in {'check', 'replenish'}
                                    or current.key in {'reverse_replenish_buy', 'reverse_replenish_sell'}))
            timestamp_rejected = (self.automatic and isinstance(exc, MexcAPIError)
                                  and exc.code == 700003 and exc.http_status == 400)
            status = ("waiting" if transient_adspower or transient_mexc_read or transient_chat
                      or transient_mutation or transient_click or timestamp_rejected
                      or isinstance(exc, CashVolumeLimitReached) else
                      "stopped" if isinstance(exc, (OperatorStopped, KeyboardInterrupt)) else
                      "paused" if isinstance(exc, (Paused, KeyboardInterrupt)) else "error")
            reason = str(exc) if isinstance(exc, (Paused, MexcAPIError, ValueError)) else type(exc).__name__
            if isinstance(exc, AdsPowerError):
                reason = str(exc)
            self.journal.transition(cycle_id, current.key, current.actor, status,
                f"{current.label}: {reason[:700]}",
                context=self.context(current.key), cycle_status="paused")
            if isinstance(exc, Exception) and not (transient_adspower or transient_mexc_read or transient_chat
                                                   or transient_mutation or transient_click
                                                   or isinstance(exc, CashVolumeLimitReached)):
                await self.reporter.flush()
            raise

    def daily_limit_message(self) -> str:
        sale = self.result('forward_complete')
        detail = (f" Первая продажа завершена: {sale['quantity']} USDT; "
                  "обратный ордер не создан, пополнение объявления не выполнено." if sale else "")
        return (f"MEXC 60085: дневной торговый лимит П2 ({self.p2_profile}). "
                "Серия приостановлена; автоматического повтора и смены аккаунта нет."
                + detail + " Проверьте лимит/уровень KYC в MEXC. После снятия ограничения "
                f"продолжите: cycle --resume {self.cycle_id} --interactive.")

    def ad_rejection_message(self) -> str:
        sale = self.result('forward_complete')
        detail = (f' Первая продажа завершена: {sale["quantity"]} USDT; '
                  'обратный P2P-ордер не создан, требуется сверенный возврат USDT.' if sale else
                  ' Первая сделка не создана; возврат USDT не нужен.')
        return (f'MEXC 85010: П2 ({self.p2_profile}) не может создать ордер '
                'по объявлению П1. Это отказ по конкретному объявлению, а не подтверждённый '
                'суточный лимит.' + detail)

    async def close_completed_tabs(self):
        if not self.browser or self.spec["mode"] != "api":
            return
        required = ["forward_complete", "reverse_complete", "reverse_replenish"]
        if self.spec.get("buy_replenish"):
            required.append("reverse_replenish_buy")
        if not all(self.result(key) for key in required):
            return
        try:
            orders = [self.result(f"{leg}_create")["order_no"] for leg in ("forward", "reverse")]
            closed = await self.browser.close_order_tabs(orders)
            if self.maker_browser and self.spec.get('reverse_maker') == 'p2':
                closed += await self.maker_browser.close_order_tabs(orders)
            self.console.write(f"Закрыто вкладок завершённых ордеров: {closed}.")
        except Exception as exc:
            # Trading is already completed; tab cleanup must not cause a replay.
            self.console.write(f"Цикл завершён, но вкладки закрыть не удалось ({type(exc).__name__}).")
        if (self.maker_browser and self.spec.get('reverse_maker') == 'p2'
                and getattr(self.maker_browser, 'profile_id', None)
                != getattr(self.browser, 'profile_id', None)):
            try:
                await self.maker_browser.stop_profile()
                self.console.write('Временный профиль П2 AdsPower закрыт.')
            except Exception as exc:
                self.console.write(f'Цикл завершён, но профиль П2 закрыть не удалось ({type(exc).__name__}).')

    async def guard_participants(self):
        """Only our saved order IDs; unrelated orders never enter this workflow."""
        for leg in ("forward", "reverse"):
            if self.result(f"{leg}_create"):
                await self.snapshot(leg)

    def check_counterparty(self, detail: dict, actor: str, order_no: str):
        expected_actor = "p2" if actor == "p1" else "p1"
        expected_id = self.trusted_members.get(expected_actor)
        if not expected_id and not self.automatic:
            return
        member_id, nickname = counterparty_identity(detail)
        if not member_id or member_id != expected_id:
            raise Paused(f"Ордер {order_no}, ответ {actor}: ID контрагента не совпал или отсутствует. Действие заблокировано.")
        expected_nickname = self.trusted_nicknames.get(expected_actor)
        if (expected_actor == "p2" or expected_nickname) and (
                not nickname or not expected_nickname or nickname != expected_nickname):
            raise Paused(f"Ордер {order_no}, ответ {actor}: ник контрагента не совпал или отсутствует. Действие заблокировано.")

    async def reconcile_unknown_create(self, step: Step, plan: dict) -> bool:
        """Find a lost taker order before allowing another create request."""
        leg = step.key.split('_', 1)[0]
        if (not isinstance(plan, dict) or not plan.get('adv_no')
                or not (plan.get('amount') or plan.get('tradable_quantity'))
                or plan['adv_no'] != self.result(f'{leg}_ad')['adv_no']):
            raise Paused('Неизвестный результат создания ордера: параметры прежнего запроса не сохранены')
        row = self.journal.db.execute(
            "SELECT time FROM events WHERE cycle_id=? AND step=? AND status='in_flight' ORDER BY id LIMIT 1",
            (self.cycle_id, step.key)).fetchone()
        if not row:
            raise Paused('В журнале нет времени отправки ордера; автоматическое создание дубля запрещено')
        start_ms = int(datetime.fromisoformat(row[0]).timestamp() * 1000) - 30000
        for attempt in range(2):
            orders = await self.clients[step.actor].list_orders(
                start_time_ms=start_ms, end_time_ms=int(datetime.now(timezone.utc).timestamp() * 1000), limit=50)
            if len(orders) >= 50:
                raise Paused('История ордеров MEXC обрезана; отсутствие предыдущего ордера не доказано')
            matches = []
            for order in orders:
                if not isinstance(order, dict) or not order.get('advOrderNo') or not order.get('advNo'):
                    raise Paused('MEXC вернул неполную историю ордеров; повтор создания запрещён')
                if order['advNo'] != plan['adv_no']:
                    continue
                key = 'amount' if plan.get('amount') else 'tradableQuantity'
                try:
                    if Decimal(str(order[key])) == Decimal(str(plan.get('amount') or plan['tradable_quantity'])):
                        matches.append(str(order['advOrderNo']))
                except (KeyError, InvalidOperation, TypeError):
                    raise Paused('MEXC не вернул сумму ордера для сверки; повтор создания запрещён') from None
            if len(set(matches)) > 1:
                raise Paused('Найдено несколько одинаковых ордеров; автоматический выбор запрещён')
            if matches:
                order_no = matches[0]
                states = set()
                for actor in ('p1', 'p2'):
                    detail = await self.clients[actor].get_order_detail(order_no)
                    if (str(detail.get('advOrderNo')) != order_no
                            or str(detail.get('advNo')) != plan['adv_no']
                            or str(detail.get('coinName')).upper() != 'USDT'
                            or str(detail.get('fiatUnit')).upper() != self.spec['fiat']):
                        raise Paused('Найденный ордер не совпадает с сохранённым запросом')
                    self.check_counterparty(detail, actor, order_no)
                    key = 'amount' if plan.get('amount') else 'tradableQuantity'
                    try:
                        if Decimal(str(detail[key])) != Decimal(str(plan.get('amount') or plan['tradable_quantity'])):
                            raise Paused('Сумма найденного ордера не совпадает с сохранённым запросом')
                    except (KeyError, InvalidOperation, TypeError):
                        raise Paused('MEXC не вернул сумму найденного ордера') from None
                    states.add(str(detail.get('state')))
                if len(states) != 1:
                    raise Paused('Статусы найденного ордера различаются между П1 и П2')
                await self.event(step, 'done', 'Созданный ордер найден на MEXC после потери ответа',
                                 {'order_no': order_no})
                return True
            if attempt == 0:
                await self.wait_delay(3)
        return False

    async def snapshot(self, leg: str) -> dict:
        order_no = self.result(f"{leg}_create")["order_no"]
        if self.spec["mode"] == "api":
            details = {}
            for actor in ("p1", "p2"):
                try:
                    detail = await self.clients[actor].get_order_detail(order_no)
                except MexcReadUnavailable as exc:
                    raise MexcReadUnavailable(f'Чтение ордера через {actor}: {exc}') from exc
                if str(detail.get("advOrderNo", "")) != order_no:
                    raise ValueError("API вернул другой номер ордера; продолжение заблокировано")
                self.check_counterparty(detail, actor, order_no)
                if str(detail.get("coinName", "")).upper() != "USDT":
                    raise ValueError("Ордер не в USDT; продолжение заблокировано")
                if str(detail.get("fiatUnit", "")).upper() != self.spec["fiat"]:
                    raise ValueError("Фиатная валюта не совпадает с циклом")
                if detail.get("advNo") is not None and str(detail["advNo"]) != self.result(f"{leg}_ad")["adv_no"]:
                    raise ValueError("Объявление ордера не совпадает с выбранным")
                details[actor] = {"order_no": order_no, "amount": money(detail.get("amount", "")),
                    "quantity": money(detail.get("tradableQuantity", "")), "fiat": self.spec["fiat"],
                    "state": str(detail.get("state", "UNKNOWN"))}
            a, b = details["p1"], details["p2"]
            if Decimal(a["amount"]) != Decimal(b["amount"]) or Decimal(a["quantity"]) != Decimal(b["quantity"]):
                raise ValueError("Данные ордера в двух аккаунтах не совпадают")
            if a["state"] != b["state"]:
                raise Paused(f"Статусы ордера пока не совпадают: П1={a['state']}, П2={b['state']}. Повторите продолжение позже.")
            snapshot = a
        else:
            previous = self.result(f"{leg}_verify")
            snapshot = {"order_no": order_no, "fiat": self.spec["fiat"], "state": "operator_verified",
                "amount": self.console.ask("Фактическая сумма ордера в фиате",
                    previous.get("amount", self.spec["amount"] if leg == "forward" else ""), money),
                "quantity": self.console.ask("Фактическое количество USDT в ордере",
                    previous.get("quantity", self.result("forward_complete").get("quantity", "")), money)}
        if leg == "forward" and Decimal(snapshot["amount"]) != Decimal(self.spec["amount"]):
            raise ValueError("Сумма первой сделки не совпадает с согласованной")
        if leg == "reverse":
            expected_quantity = Decimal(self.result("forward_complete")["quantity"])
            actual_quantity = Decimal(snapshot["quantity"])
            residual = expected_quantity - actual_quantity
            if self.spec.get('reverse_maker') == 'p2':
                # A P1 BUY order is submitted in fiat cents; MEXC can round the
                # resulting USDT down by a fraction of a cent's worth.
                if residual < 0 or residual > Decimal('0.0003'):
                    raise ValueError('Обратная сделка не покрывает первую; требуется сверка остатка USDT П2')
                snapshot['residual_usdt'] = str(residual)
            elif residual != 0:
                raise ValueError("Обратная сделка должна продавать количество USDT, полученное в первой")
        prior = self.result(f"{leg}_verify")
        if prior and any(Decimal(snapshot[k]) != Decimal(prior[k]) for k in ("amount", "quantity")):
            raise ValueError("Параметры сделки изменились после сверки")
        # Suppress duplicate output only; all identity/state checks above still run.
        if self.spec["mode"] == "api" and self._last_reported.get(leg) != snapshot:
            direction = "Первая сделка" if leg == "forward" else "Обратная сделка"
            self.console.write(f"{direction}: {snapshot['order_no']} — {snapshot['amount']} {snapshot['fiat']}, "
                               f"{snapshot['quantity']} USDT; статус {snapshot['state']} (П1 и П2 сверены).")
            self._last_reported[leg] = dict(snapshot)
        return snapshot

    def check_state(self, snapshot: dict, expected: set[str]):
        if self.spec["mode"] == "api" and snapshot["state"] not in expected:
            raise Paused(f"MEXC: ордер {snapshot['order_no']} имеет статус {snapshot['state']}; "
                         f"для этого шага нужен {' / '.join(sorted(expected))}. "
                         "Подтверждение в консоли не меняет статус на бирже. Продолжение остановлено.")

    def forward_pay_method_id(self) -> int | None:
        if self.spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}:
            configured = self.spec.get('eflp_p1_pay_method_id')
            if configured:
                return positive_id(str(configured))
        return self.pay_method_id

    async def payment_account(self, actor: str, order_no: str) -> int:
        detail = await self.clients[actor].get_order_detail(order_no)
        if str(detail.get("advOrderNo", "")) != order_no:
            raise ValueError("Получены реквизиты другого ордера")
        payments = detail.get("paymentInfo")
        if not isinstance(payments, list) or not payments:
            raise ValueError("MEXC не вернул paymentInfo ордера. Подтверждение оплаты через API остановлено.")
        ids = []
        for payment in payments:
            if not isinstance(payment, dict):
                raise ValueError("Неожиданный формат paymentInfo")
            # The account ID is paymentInfo.id; payMethod is the method type (e.g. 578).
            ids.append(positive_id(str(payment.get("id", ""))))
        if (actor == 'p2' and self.spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}
                and self.forward_pay_method_id()):
            selected = [ids[i] for i, payment in enumerate(payments)
                        if str(payment.get('payMethod')) == str(self.forward_pay_method_id())]
            if len(selected) != 1:
                raise Paused('Первый ордер не содержит однозначных реквизитов П1 с выбранным payMethod')
            return selected[0]
        if (actor == 'p1' and self.spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}
                and self.p2_payment_id):
            selected = [account_id for account_id in ids if account_id == positive_id(self.p2_payment_id)]
            if len(selected) != 1:
                raise Paused('Обратный ордер Eflp не содержит выбранных реквизитов П2 из .env')
            return selected[0]
        if actor == 'p1' and self.p2_pay_method_id:
            selected = [ids[i] for i, payment in enumerate(payments)
                        if str(payment.get('payMethod')) == str(self.p2_pay_method_id)]
            if len(selected) != 1:
                raise Paused('Обратный ордер не содержит однозначных реквизитов П2 с выбранным payMethod')
            return selected[0]
        if len(ids) == 1:
            self.console.write(f"ID платёжных реквизитов получен из ордера: {ids[0]}")
            return ids[0]
        if self.automatic:
            method_id = self.forward_pay_method_id() if actor == 'p2' else self.pay_method_id
            selected = [ids[i] for i, payment in enumerate(payments) if str(payment.get("payMethod")) == str(method_id)]
            if len(selected) == 1:
                return selected[0]
            raise Paused("Для оплаты найдено несколько реквизитов; нужен однозначный выбор. Продолжите с --interactive.")
        for index, payment in enumerate(payments, 1):
            self.console.write(f"{index}. Способ оплаты {payment.get('payMethod', '')}, ID реквизитов {ids[index - 1]}")
        def select(value: str) -> int:
            index = positive_id(value)
            if index > len(ids):
                raise ValueError("Выберите номер из показанного списка")
            return ids[index - 1]
        return self.console.ask("Номер варианта, которым фактически оплачена сделка", validate=select)

    async def prepare(self, step: Step, *, recovery: bool) -> dict:
        leg, action = step.key.split("_", 1)
        if step.key in {"reverse_replenish_buy", "reverse_replenish_sell"}:
            action = "replenish"
        manual = self.spec["mode"] == "manual" or recovery
        ctx = self.context(step.key)
        if action == "replenish":
            maker_preparation = step.key == 'reverse_replenish_sell'
            snapshot = await self.snapshot("forward" if maker_preparation else "reverse")
            self.check_state(snapshot, COMPLETED_STATES)
            quantity = money(self.result("forward_complete" if maker_preparation else "reverse_complete")["quantity"])
            buy_ad = step.key == "reverse_replenish_buy"
            adv_no = (self.spec['reverse_adv_no'] if maker_preparation else
                      self.result("reverse_ad" if buy_ad else "forward_ad")["adv_no"])
            browser = self.maker_browser if maker_preparation else self.browser
            if self.spec["mode"] == "manual":
                self.console.confirm(f"На MEXC добавьте {quantity} USDT к остатку объявления {adv_no}. "
                                     "Если уже добавили, повторно не пополняйте.", f"ПОПОЛНЕНО {adv_no}")
                return {"adv_no": adv_no, "quantity": quantity}
            ad = await self.clients[step.actor].get_ad(adv_no)
            if recovery:
                saved = self.journal.step(self.cycle_id, step.key)["result"]
                # Old attempts stored the cumulative target; recover the available target
                # from the original snapshot without calculating a second increment.
                if "target_available" not in saved and "before_available" in saved:
                    saved["target_available"] = str(Decimal(saved["before_available"]) + Decimal(saved["quantity"]))
                saved.pop("before_quantity", None)
                saved.pop("target_quantity", None)
                self.check_replenished(ad, saved)
                if saved.get('method') == 'quantity_only':
                    await self.check_browser_ad(saved, browser=browser)
                self.console.write(f"Доступный остаток объявления соответствует цели {saved['target_available']} USDT. "
                                   "Проверьте пополнение на MEXC; повторного запроса не будет.")
                return saved
            plan = (await self.quantity_plan(ad, adv_no, quantity, side="BUY" if buy_ad else "SELL", browser=browser)
                    if (maker_preparation or buy_ad or self.spec.get('scheduler_mode') == 'eflp_volume'
                        or ad.get('overVerify') is None) else self.replenish_plan(ad, adv_no, quantity))
            self.console.write(f"Объявление {adv_no}: сейчас {plan['before_available']} USDT, добавить {quantity} USDT. "
                               f"Ожидаемый доступный остаток: {plan['target_available']} USDT.\n"
                               f"Статус объявления: {ad.get('advStatus', 'неизвестен')}; публикация этим шагом не выполняется.")
            if "target_max_limit" in plan:
                self.console.write(f"Максимум одной сделки будет уменьшен: {plan['before_max_limit']} → "
                                   f"{plan['target_max_limit']} {self.spec['fiat']}, по стоимости объёма после пополнения. "
                                   "Подтверждение ниже разрешает и пополнение, и изменение максимума. Минимальный лимит сохраняется.")
            return plan
        if action == "ad":
            p2_maker = leg == 'reverse' and self.spec.get('reverse_maker') == 'p2'
            direction = ("ПРОДАЖА USDT: П1 — продавец" if leg == "forward" else
                         "ПРОДАЖА USDT: П2 — продавец" if p2_maker else
                         "ПОКУПКА USDT: П1 — покупатель")
            self.console.write(f"Используем готовое объявление {'П2' if p2_maker else 'П1'}. {direction}.\n"
                               "Проверьте владельца, валюту, цену, лимиты, доступный остаток и способ оплаты.")
            adv_no = self.spec.get(f"{leg}_adv_no")
            if not adv_no:
                adv_no = self.console.ask("Номер готового объявления П1 (advNo)")
            if self.automatic:
                ad = await self.clients[step.actor].get_ad(adv_no)
                if (ad.get("advNo") != adv_no or ad.get("side") != ("SELL" if leg == "forward" or p2_maker else "BUY")
                        or ad.get("coinName") != "USDT" or ad.get("fiatUnit") != self.spec["fiat"]):
                    raise Paused("Объявление не соответствует владельцу, стороне сделки, токену или валюте")
                if leg == 'forward' and self.spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}:
                    configured_method = self.forward_pay_method_id()
                    payments = ad.get('paymentInfo')
                    if not isinstance(payments, list) or not payments or not all(isinstance(p, dict) for p in payments):
                        raise Paused('MEXC не вернул способы оплаты объявления П1; первый ордер не открыт')
                    if str(configured_method) not in {str(p.get('payMethod')) for p in payments}:
                        raise Paused(f'Способ оплаты {configured_method} не найден в объявлении П1; первый ордер не открыт')
                if (leg == "forward" or p2_maker) and self.spec.get('scheduler_mode') not in {'eflp_volume', 'eflp_unique'}:
                    # A configured fallback is not evidence that the live checkbox is on.
                    browser = self.maker_browser if p2_maker else self.browser
                    actual = await browser.ad_details(adv_no) if ad.get('overVerify') is None else ad
                    ad_verification(actual)
            if leg == "forward":
                self.console.write(f"Объявление: {adv_no}. Покупатель: П2. Сумма покупки: {self.spec['amount']} {self.spec['fiat']}.")
            return {"adv_no": adv_no, "direction": direction}
        if action == "create":
            if manual:
                self.console.write("Найдите уже созданную сделку на MEXC; не создавайте дубликат. Если результат неясен — STOP."
                                   if recovery else "Откройте сделку на MEXC от имени П2 по выбранному объявлению П1.")
                order_no = self.console.ask("Номер уже открытого ордера")
                if leg == "reverse" and order_no == self.result("forward_create")["order_no"]:
                    raise ValueError("Обратная сделка должна иметь новый номер ордера")
                return {"order_no": order_no}
            args = {"adv_no": self.result(f"{leg}_ad")["adv_no"]}
            if leg == "forward":
                if self.spec.get('cash_policy') == 'rolling24_p1':
                    from volume_policy import (CASH_TARGET_USDT, CASH_CEILING_USDT,
                                               rolling_cash_purchases)
                    live_ad = await self.clients['p1'].get_ad(args['adv_no'])
                    price = Decimal(str(live_ad.get('price', 'NaN')))
                    if (live_ad.get('advNo') != args['adv_no'] or live_ad.get('side') != 'SELL'
                            or live_ad.get('advStatus') != 'OPEN' or not price.is_finite()
                            or price <= 0):
                        raise Paused('Объявление П1 изменилось; первая покупка не отправлена')
                    window = rolling_cash_purchases(self.journal, self.p2_profile,
                                                     member_id=self.trusted_members.get('p2'))
                    projected = Decimal(self.spec['amount']) / price * Decimal('1.005')
                    if (window.get('uncertain_until')
                            or Decimal(window['quantity']) >= CASH_TARGET_USDT
                            or Decimal(window['quantity']) + projected > CASH_CEILING_USDT):
                        raise CashVolumeLimitReached('Скользящий объём П2 достиг лимита 69–70 тыс. USDT; '
                                                     'новая покупка не отправлена')
                self.console.write(f"После подтверждения П2 откроет сделку по объявлению {args['adv_no']} "
                                   f"на {self.spec['amount']} {self.spec['fiat']}.")
                args.update(amount=self.spec["amount"], user_confirm_pay_method_id=self.forward_pay_method_id() or self.console.ask(
                    "ID способа оплаты из объявления (не номер карты)", validate=positive_id))
            elif self.spec.get('reverse_maker') == 'p2':
                ad = await self.clients['p2'].get_ad(args['adv_no'])
                price = Decimal(str(ad.get('price', 'NaN')))
                quantity = Decimal(self.result('forward_complete')['quantity'])
                available = Decimal(str(ad.get('availableQuantity', 'NaN')))
                if (not price.is_finite() or price <= 0 or not available.is_finite()
                        or available < quantity):
                    raise Paused('Объявление продажи П2 не покрывает количество обратной сделки')
                amount = (price * quantity).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
                minimum = Decimal(str(ad.get('minSingleTransAmount', 'NaN')))
                maximum = Decimal(str(ad.get('maxSingleTransAmount', 'NaN')))
                if (not minimum.is_finite() or not maximum.is_finite()
                        or not minimum <= amount <= maximum):
                    raise Paused('Сумма обратной сделки вне лимитов объявления П2; настройте объявление или разделите возврат')
                args.update(amount=str(amount), user_confirm_pay_method_id=self.pay_method_id)
            else:
                if self.p2_pay_method_id:
                    if not self.p2_payment_browser:
                        raise Paused('Для получения ID реквизитов П2 по payMethod нужен его профиль AdsPower')
                    payment_id = self.p2_payment_account_id or await self.p2_payment_browser.payment_account_by_method(
                        self.p2_pay_method_id, self.spec['fiat'])
                    self.console.write(f'Реквизиты П2 для {self.spec["fiat"]} определены по payMethod {self.p2_pay_method_id}.')
                elif self.p2_payment_id:
                    payment_id = positive_id(self.p2_payment_id)
                    self.console.write(f"ID реквизитов П2 из выбранного профиля {self.p2_profile}: {payment_id}. Проверьте, что реквизиты актуальны.")
                else:
                    self.console.write("Нужен ID сохранённых и активных реквизитов в аккаунте П2.\n"
                                       "578 — код способа «Личный платёж наличными» (payMethod), а не ID реквизитов.\n"
                                       "Возьмите id записи в способах оплаты П2; ID из объявления П1 здесь не подходит.\n"
                                       "Чтобы не вводить его снова, сохраните ID в .env: MEXC_P2_PAYMENT_ID=ваш_ID")
                    payment_id = self.console.ask(
                        "ID платёжных реквизитов П2 для получения денег (не номер карты)", validate=positive_id)
                args.update(tradable_quantity=self.result("forward_complete")["quantity"],
                            user_confirm_payment_id=payment_id)
            return args
        if action == "verify":
            snapshot = await self.snapshot(leg)
            roles = "П1 — продавец, П2 — покупатель" if leg == "forward" else "П1 — покупатель, П2 — продавец"
            self.console.confirm((f"Данные ордера {snapshot['order_no']} получены через API обоих аккаунтов. Подтвердите:\n"
                if not manual else f"Откройте ордер {snapshot['order_no']} в обоих аккаунтах и сверьте:\n") +
                f"{roles}; объявление {self.result(f'{leg}_ad')['adv_no']}; "
                f"{snapshot['amount']} {snapshot['fiat']}; {snapshot['quantity']} USDT; реквизиты получателя.")
            return snapshot
        if action in {"message", "reply"}:
            if self.automatic:
                from phrases import PHRASES
                selected = self.spec.get('eflp_phrases') or PHRASES
                text = random.choice(selected[step.key])
            else:
                text = self.console.ask("Текст сообщения", "Здравствуйте! Готов к сделке." if action == "message" else "Здравствуйте! Вижу ваш ордер.")
            if len(text) > 2000:
                raise ValueError("Сообщение длиннее 2000 символов")
            if manual:
                self.console.write(f"Отправьте этот текст в чат ордера {ctx['order_no']} от имени {step.actor}.")
            return {"order_no": ctx["order_no"], "text": text}
        if action == "check":
            browser = self.browser_for_step(step)
            if browser and self.spec["mode"] == "api" and recovery:
                if await browser.inspect(ctx["order_no"]) not in {"passed", "not_required"}:
                    raise Paused("Предыдущее нажатие не подтверждено страницей MEXC. Завершите проверку на сайте; повторного нажатия не будет.")
            if browser and not manual:
                self.check_state(await self.snapshot(leg), {"NOT_PAID"})
                state = await browser.open_order(ctx["order_no"])
                if state == "not_required":
                    self.console.write("MEXC явно сообщает: дополнительная проверка для этого ордера не требуется.")
                    return {"order_no": ctx['order_no'], "verification": "not_required"}
                self.console.confirm(f"Проверьте документы по ордеру {ctx['order_no']}. "
                    f"После подтверждения бот нажмёт «Проверка пройдена» в профиле {step.actor.upper()} AdsPower.\n"
                    + ("На странице уже показано ожидание оплаты; повторного нажатия не будет." if state == "passed" else ""),
                    f"ДОКУМЕНТЫ ПРОВЕРЕНЫ {ctx['order_no']}")
                return {"order_no": ctx["order_no"], "verification": "adspower"}
            self.console.confirm(f"В аккаунте {step.actor.upper()} выполните проверку продавца по ордеру {ctx['order_no']} "
                "и нажмите на MEXC «Проверка пройдена».\nПодтвердите, что действие выполнено на бирже.",
                f"ПРОВЕРКА ПРОЙДЕНА {ctx['order_no']}")
            return {"order_no": ctx["order_no"], "verification": "seller_check_completed_on_mexc"}
        if action in {"paid", "release"}:
            # Re-read both accounts before every financial operation; never use PAID as bank proof.
            fresh = await self.snapshot(leg)
            expected = (PAID_STATES if recovery else PAYMENT_START_STATES[leg]) if action == "paid" else (COMPLETED_STATES if recovery else {"PAID"})
            self.check_state(fresh, expected)
            self.console.write(f"Ордер {fresh['order_no']}: {fresh['amount']} {fresh['fiat']}, {fresh['quantity']} USDT.")
            if action == "paid":
                result = {"order_no": ctx["order_no"]}
                if not manual:
                    result["payment_account_id"] = await self.payment_account(step.actor, ctx["order_no"])
                self.console.confirm(f"{step.actor}: " + ("проверьте уже выполненную оплату; не платите повторно. "
                    if recovery else "подтвердите, что полная сумма уже оплачена выбранным способом (наличные или перевод). ") +
                    "Эта отметка не переводит деньги. Не оплачивайте сделку повторно.", f"ОПЛАТА ВЫПОЛНЕНА {ctx['order_no']}")
                if manual:
                    self.console.write("Проверьте на MEXC, что оплата уже отмечена." if recovery else
                                       "Нажмите «Оплачено» на MEXC и затем подтвердите завершение шага.")
                return result
            self.console.confirm(f"{step.actor}: проверьте фактическое получение полной суммы "
                f"{fresh['amount']} {fresh['fiat']} от покупателя: пересчитайте наличные либо проверьте банковский счёт.",
                f"ДЕНЬГИ ПОЛУЧЕНЫ {ctx['order_no']}")
            result = {"order_no": ctx["order_no"]}
            if manual:
                self.console.write("Проверьте на MEXC, что USDT уже выпущены." if recovery else
                                   "Выпустите USDT на MEXC и затем подтвердите завершение шага.")
            else:
                method = "NONE" if self.automatic else self.console.ask("Проверка MEXC для выпуска: NONE / GA / SMS / MAIL", "NONE").upper()
                if method not in {"NONE", "GA", "SMS", "MAIL"}:
                    raise ValueError("Неизвестный способ подтверждения")
                if method != "NONE":
                    result["notify_type"] = method
                    result["notify_code"] = getpass("Код MEXC (не сохраняется): ").strip()
                    if not result["notify_code"]:
                        raise ValueError("Пустой код подтверждения")
            return result
        if action == "complete":
            snapshot = await self.snapshot(leg)
            self.check_state(snapshot, COMPLETED_STATES)
            self.console.confirm(("MEXC подтвердил через API: " if not manual else "Проверьте на MEXC: ") +
                f"ордер {snapshot['order_no']} ЗАВЕРШЁН, "
                f"{snapshot['quantity']} USDT зачислены покупателю. "
                + ("П2 может продать это количество в обратной сделке." if leg == "forward" else ""),
                f"ЗАВЕРШЕНО {snapshot['order_no']}")
            return snapshot
        raise ValueError(f"Неизвестный шаг {step.key}")

    async def execute(self, step: Step, data: dict, *, recovery: bool) -> dict:
        leg, action = step.key.split("_", 1)
        if step.key in {"reverse_replenish_buy", "reverse_replenish_sell"}:
            action = "replenish"
        if self.spec["mode"] == "manual":
            return data
        if action in {"message", "reply", "check", "paid", "release", "complete"}:
            if data.get("order_no") != self.result(f"{leg}_create").get("order_no"):
                raise Paused("Номер ордера действия не совпадает с сохранённым ордером цикла")
        if self.automatic and action in MUTATING_ACTIONS and not recovery:
            await self.guard_participants()
            if action in {"message", "reply"}:
                self.check_state(await self.snapshot(leg), PAYMENT_START_STATES[leg])
        if action == "replenish":
            maker_preparation = step.key == 'reverse_replenish_sell'
            self.check_state(await self.snapshot("forward" if maker_preparation else "reverse"), COMPLETED_STATES)
            client = self.clients[step.actor]
            browser = self.maker_browser if maker_preparation else self.browser
            ad = await client.get_ad(data["adv_no"])
            if recovery:
                self.check_replenished(ad, data)
                if data.get('method') == 'quantity_only':
                    await self.check_browser_ad(data, browser=browser)
                return data
            if data.get('method') == 'quantity_only':
                try:
                    current = await self.quantity_plan(ad, data['adv_no'], data['quantity'],
                                                       side=data.get('side', 'SELL'), browser=browser)
                except AdsPowerUnavailable as exc:
                    raise AdsPowerPreflightUnavailable(str(exc)) from None
                if ('target_total' in data and 'target_total' in current):
                    same = all(current.get(key) == data.get(key) for key in
                               ('adv_no', 'quantity', 'fiat', 'side', 'over_verify', 'target_total'))
                else:
                    same = current == data
                if not same:
                    raise Paused("Объявление изменилось после подтверждения; пополнение не отправлено")
                try:
                    await browser.replenish_ad(data)
                except AdsPowerUnavailable as exc:
                    raise AdsPowerPreflightUnavailable(str(exc)) from None
                self.check_replenished(await client.get_ad(data['adv_no']), data)
                await self.check_browser_ad(data, browser=browser)
                return data
            if self.replenish_plan(ad, data["adv_no"], data["quantity"]) != data:
                raise Paused("Объявление изменилось после подтверждения. Пополнение не отправлено; проверьте его на MEXC.")
            await client.replenish_ad(ad | {"overVerify": data["over_verify"]}, data["quantity"])
            self.check_replenished(await client.get_ad(data["adv_no"]), data)
            return data
        # Recheck after console input: the order may have changed while the operator was reading.
        if action in {"paid", "release", "complete"}:
            expected = (PAYMENT_START_STATES[leg] if action == "paid" else {"PAID"})
            if recovery or action == "complete":
                expected = PAID_STATES if action == "paid" else COMPLETED_STATES
            self.check_state(await self.snapshot(leg), expected)
        check_browser = self.browser_for_step(step) if action == 'check' else None
        if recovery and action == "check" and check_browser:
            if await check_browser.inspect(data["order_no"]) not in {"passed", "not_required"}:
                raise Paused("Страница MEXC не подтверждает завершение проверки документов")
        if recovery:
            return data
        if action == "check" and data.get("verification") == "not_required":
            if await check_browser.inspect(data['order_no']) != 'not_required':
                raise Paused("Требование проверки изменилось; продолжение остановлено")
            return data
        if action == "check" and data.get("verification") == "adspower":
            self.check_state(await self.snapshot(leg), {"NOT_PAID"})
            await check_browser.approve(data["order_no"])
            return {"order_no": data["order_no"], "verification": "seller_check_completed_on_mexc"}
        client = self.clients[step.actor] if step.actor != "both" else None
        if action == "create":
            return {"order_no": await client.create_order(**data)}
        if action in {"message", "reply"}:
            await client.send_chat_text(data["order_no"], data["text"])
            receiver = "p1" if step.actor == "p2" else "p2"
            # Placeholder until a verified read-receipt mechanism is configured.
            data["chat_read"] = await self.clients[receiver].mark_chat_read(data["order_no"])
        elif action == "paid":
            seller_browser = (None if self.spec.get('scheduler_mode') in {'eflp_volume', 'eflp_unique'}
                              else self.browser if leg == 'forward' else self.maker_browser
                              if self.spec.get('reverse_maker') == 'p2' else None)
            if seller_browser:
                try:
                    verification = await seller_browser.inspect(data["order_no"])
                except (AdsPowerTimeout, AdsPowerUnavailable) as exc:
                    raise AdsPowerPreflightUnavailable(str(exc)) from exc
                if verification not in {"passed", "not_required"}:
                    raise Paused("Дополнительная проверка документов ещё не пройдена на сервере MEXC; отметка оплаты не отправлена")
            await client.mark_paid(data["order_no"], data["payment_account_id"])
            self.check_state(await self.snapshot(leg), PAID_STATES)
        elif action == "release":
            await client.release_coin(data["order_no"], notify_type=data.get("notify_type"), notify_code=data.get("notify_code"))
            self.check_state(await self.snapshot(leg), COMPLETED_STATES)
        return {k: v for k, v in data.items() if k != "notify_code"}

    async def check_browser_ad(self, plan: dict, browser=None):
        browser = browser or self.browser
        if not browser:
            raise Paused("Для пополнения без сброса настроек нужен открытый профиль П1 AdsPower")
        actual = await browser.ad_details(plan['adv_no'])
        verification = CycleRunner.browser_ad_verification(self, actual, plan.get('side', 'SELL'))
        if 'target_total' in plan:
            matched = (Decimal(str(actual.get('availableQuantity', 'NaN')))
                       + Decimal(str(actual.get('frozenQuantity', 'NaN')))
                       == Decimal(plan['target_total']))
        else:
            matched = Decimal(str(actual.get('availableQuantity', 'NaN'))) == Decimal(plan['target_available'])
        if str(actual.get('id')) != plan['adv_no'] or verification != plan['over_verify'] or not matched:
            raise Paused("Остаток или дополнительная проверка не совпадают с планом; повторное пополнение запрещено")

    def browser_ad_verification(self, actual: dict, side: str) -> str:
        if side == 'BUY':
            return json.dumps(actual.get('overVerify'), sort_keys=True, ensure_ascii=False)
        if self.spec.get('scheduler_mode') == 'eflp_volume':
            # Eflp volume ads need no identity-document requirement. Preserve
            # whatever is actually configured, including a missing check.
            value = actual.get('overVerify')
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise Paused('Некорректная настройка проверки объявления П1') from None
            return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return ad_verification(actual)

    async def quantity_plan(self, ad: dict, adv_no: str, quantity: str, side: str = 'SELL', browser=None) -> dict:
        browser = browser or self.browser
        if not browser:
            raise Paused("API не возвращает настройки проверки. Для пополнения без их сброса нужен AdsPower П1")
        actual = await browser.ad_details(adv_no)
        api_frozen = ad.get('frozenQuantity')
        browser_frozen = actual.get('frozenQuantity')
        if api_frozen is not None and browser_frozen is not None:
            balance_matches = (Decimal(str(actual.get('availableQuantity', 'NaN'))) + Decimal(str(browser_frozen))
                               == Decimal(str(ad.get('availableQuantity', 'NaN'))) + Decimal(str(api_frozen)))
        else:
            balance_matches = (Decimal(str(actual.get('availableQuantity', 'NaN')))
                               == Decimal(str(ad.get('availableQuantity', 'NaN'))))
        if (actual.get('id') != adv_no or actual.get('coinName') != 'USDT'
                or actual.get('tradeType') != (0 if side == 'BUY' else 1)
                or ad.get('side', side) != side
                or actual.get('currency') != self.spec['fiat'] or ad.get('advNo') != adv_no
                or not balance_matches):
            raise Paused("Данные объявления в API и браузере расходятся; пополнение остановлено")
        available = Decimal(str(actual['availableQuantity']))
        increment = Decimal(money(quantity))
        if not available.is_finite() or available < 0:
            raise ValueError("Некорректный остаток объявления")
        verification = CycleRunner.browser_ad_verification(self, actual, side)
        plan = {'method': 'quantity_only', 'adv_no': adv_no, 'fiat': self.spec['fiat'], 'quantity': str(increment),
                'before_available': str(available), 'target_available': str(available + increment),
                'over_verify': verification, 'side': side}
        if api_frozen is not None and browser_frozen is not None:
            frozen = Decimal(str(browser_frozen))
            if not frozen.is_finite() or frozen < 0:
                raise Paused('Некорректный замороженный остаток объявления')
            plan.update(before_frozen=str(frozen), before_total=str(available + frozen),
                        target_total=str(available + frozen + increment))
        return plan

    def replenish_plan(self, ad: dict, adv_no: str, quantity: str) -> dict:
        if ad.get("advNo") != adv_no or ad.get("fiatUnit") != self.spec["fiat"]:
            raise ValueError("Объявление или валюта не совпадают с исходной продажей")
        params = ad_replenish_params(ad | {"overVerify": ad_verification(ad, self.p1_over_verify)}, quantity)
        available = Decimal(str(ad.get("availableQuantity", "")))
        if not available.is_finite() or available < 0:
            raise ValueError("Некорректный остаток объявления")
        plan = {"adv_no": adv_no, "quantity": quantity,
                "before_available": str(available), "target_available": str(available + Decimal(quantity)),
                "over_verify": params["overVerify"],
                "settings_fingerprint": fingerprint(json.dumps(params, sort_keys=True, ensure_ascii=False))}
        if Decimal(str(params["maxSingleTransAmount"])) != Decimal(str(ad["maxSingleTransAmount"])):
            plan.update(before_max_limit=str(ad["maxSingleTransAmount"]),
                        target_max_limit=str(params["maxSingleTransAmount"]))
        return plan

    def check_replenished(self, ad: dict, plan: dict):
        # Only compare an actual exchange field. The fallback is not remote evidence.
        if plan.get("over_verify") and ad.get("overVerify") is not None:
            verification = (json.dumps(ad['overVerify'], sort_keys=True, ensure_ascii=False)
                            if plan.get('side') == 'BUY' else ad_verification(ad))
            if verification != plan["over_verify"]:
                raise Paused("Настройка дополнительной проверки после пополнения изменилась; повтор пополнения запрещён. Проверьте объявление на MEXC")
        if 'target_total' in plan:
            matched = (Decimal(str(ad.get('availableQuantity', 'NaN')))
                       + Decimal(str(ad.get('frozenQuantity', 'NaN'))) == Decimal(plan['target_total']))
        else:
            matched = Decimal(str(ad.get('availableQuantity', 'NaN'))) == Decimal(plan['target_available'])
        if (ad.get("advNo") != plan.get("adv_no") or ad.get("side") != plan.get("side", "SELL")
                or ad.get("coinName") != "USDT" or ad.get("fiatUnit") != self.spec["fiat"]
                or "target_available" not in plan
                or not matched
                or ("target_max_limit" in plan and Decimal(str(ad.get("maxSingleTransAmount", "NaN"))) != Decimal(plan["target_max_limit"]))):
            raise Paused("Доступный остаток или максимальный лимит объявления не совпадает с подтверждённым планом. Автоматического повтора не будет: "
                         "сверьте пополнение и новые сделки на MEXC. Не добавляйте USDT повторно, если пополнение уже выполнено.")


async def run_command(args, *, stop_event: asyncio.Event | None = None, use_lock: bool = True,
                      notify_prepare_errors: bool = True, telegram_keyboard=None) -> int:
    import os
    from pathlib import Path
    from contextlib import nullcontext
    from config import PROJECT_DIR, Settings, p2_prefix, p2_profile_name, select_p2_profile
    from journal import process_lock
    from logger_setup import setup_logging
    from mexc_client import MexcP2PClient
    from notifier import TelegramNotifier
    from sheets import GoogleSheets

    settings = Settings.from_env(require_keys=False)
    setup_logging(settings.log_dir, settings.log_level)
    path = Path(os.getenv("CYCLE_DB", "data/cycles.sqlite3"))
    if not path.is_absolute():
        path = PROJECT_DIR / path
    with process_lock(path.with_suffix(".lock")) if use_lock else nullcontext():
        journal = Journal(path)
        clients = {}
        telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        runner_started = False
        try:
            if args.command == "cycle-status":
                for row in journal.cycles():
                    print(f"{row['id']} | {row['created']} | {row['status']}")
                    spec = journal.cycle(row['id'])["spec"]
                    print(f"  П2: профиль {spec.get('p2_profile', 'default')}; ник {spec.get('nicknames', {}).get('p2', 'не сохранён')}")
                    series = spec.get("series")
                    if series:
                        print(f"  Серия: цикл {series['index']} из {series['count']}")
                    if row["status"] in {"completed", "abandoned"}:
                        continue
                    pending = next((s for s in steps_for_spec(spec) if not journal.step(row['id'], s.key)
                                    or journal.step(row['id'], s.key)['status'] != 'done'), None)
                    if pending:
                        print(f"  Следующий шаг: {pending.key} — {pending.label}")
                if not journal.cycles():
                    print("Циклов пока нет.")
                return 0
            if args.command == "cycle-reset":
                row = journal.cycle(args.cycle_id)
                if row["status"] in {"completed", "abandoned"}:
                    print("Этот цикл уже завершён или сброшен. Можно запускать новый.")
                    return 0
                for leg in ("forward", "reverse"):
                    saved = journal.step(args.cycle_id, f"{leg}_create")
                    if saved:
                        print(f"{leg}: ордер {saved['result'].get('order_no', 'номер не сохранён')}; шаг {saved['status']}")
                Console().confirm(
                    f"Сбросить цикл {args.cycle_id} в программе? История и суммы продаж сохранятся.\n"
                    "Ордера на MEXC НЕ отменяются. Перед новой сделкой проверьте старые ордера на бирже.",
                    f"СБРОСИТЬ {args.cycle_id}")
                journal.abandon(args.cycle_id)
                print("Цикл сброшен. Запуск нового: python main.py cycle")
                return 0
            if args.command == "cycle" and not args.resume:
                journal.ensure_can_create()
            sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
            credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
            if bool(sheet_id) != bool(credentials):
                raise ValueError("Заполните вместе GOOGLE_SHEET_ID и GOOGLE_SERVICE_ACCOUNT_FILE")
            sheets = None
            if sheet_id:
                credentials_path = Path(credentials)
                if not credentials_path.is_absolute():
                    credentials_path = PROJECT_DIR / credentials_path
                sheets = GoogleSheets(sheet_id, os.getenv("GOOGLE_SHEET_TAB", "Продажи"), str(credentials_path))
            if bool(settings.telegram_bot_token) != bool(settings.telegram_chat_id):
                raise ValueError("Заполните вместе TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
            reporter = Reporter(journal, telegram, sheets, keyboard=telegram_keyboard)
            if args.command == "sync-journal":
                pending = await reporter.flush(force_sheets=True)
                print(f"В очереди: Telegram — {pending['telegram']}, суммы продаж — {pending['sales']}.")
                return 0 if (not telegram.enabled or not pending['telegram']) and (not sheets or not pending['sales']) else 1
            existing = journal.cycle(args.resume) if args.resume else None
            p2_profile = select_p2_profile(args.p2_profile, existing["spec"] if existing else None, os.environ)
            p1_profile = (existing['spec'].get('p1_profile', 'p1') if existing else
                          getattr(args, 'p1_profile', None) or 'p1')
            scheduler_mode = (existing['spec'].get('scheduler_mode') if existing else
                               getattr(args, 'scheduler_mode', None))
            eflp_p1_pay_method_id = None
            if scheduler_mode == 'cash_volume':
                cash_route_setting = (existing['spec'].get('cash_final_return', 'p2p') if existing else
                                      'ordinary')
            else:
                cash_route_setting = None
            reverse_maker = (existing['spec'].get('reverse_maker', 'p1') if existing else
                             getattr(args, 'reverse_maker', None) or 'p1')
            if existing and (getattr(args, 'p1_profile', None) not in {None, p1_profile}
                             or getattr(args, 'reverse_maker', None) not in {None, reverse_maker}):
                raise ValueError('Роли участников начатого цикла менять нельзя')
            if existing and getattr(args, 'scheduler_mode', None) not in {
                    None, existing['spec'].get('scheduler_mode')}:
                raise ValueError('Режим сохранённого цикла менять нельзя')
            if p1_profile != 'p1':
                p1_profile = p2_profile_name(p1_profile)
            from trade_profiles import (eflp_p1_fiat, eflp_p2_payment_id, mode_pay_method_id,
                                        profile_prefix, settings_for_profile)
            prefixes = {"p1": profile_prefix(p1_profile),
                        "p2": p2_prefix(p2_profile)}
            automatic = (existing["spec"].get("automatic", False) if existing else args.auto) and not args.interactive
            series_options = any(v is not None for v in (args.min_amount, args.max_amount, args.count, args.fiat))
            if existing and (series_options or args.amount):
                raise ValueError("При --resume используются сохранённые суммы и количество циклов; новые параметры не задавайте")
            if series_options and not automatic:
                raise ValueError("Диапазон суммы и --count доступны только с --auto")
            plan = auto_plan(args, os.environ) if automatic and not existing else None
            p2_pay_method_id = ''
            if existing:
                p2_payment_id = existing['spec'].get('p2_payment_id', '')
                p2_pay_method_id = existing['spec'].get('p2_pay_method_id', '')
            elif scheduler_mode in {'eflp_volume', 'eflp_unique'}:
                selected_fiat = plan['fiat'] if plan else purchase_amount(args.amount or '')[1]
                expected_fiat = eflp_p1_fiat(p1_profile, os.environ)
                if selected_fiat != expected_fiat:
                    raise ValueError(f'{prefixes["p1"]}_FIAT={expected_fiat}, '
                                     f'но для ордера выбрана валюта {selected_fiat}')
                eflp_p1_pay_method_id = positive_id(mode_pay_method_id(
                    scheduler_mode, selected_fiat, os.environ))
                p2_payment_id = eflp_p2_payment_id(p2_profile, selected_fiat, os.environ)
            else:
                p2_payment_id = os.getenv(f"{prefixes['p2']}_PAYMENT_ID", "").strip()
            if p2_payment_id:
                positive_id(p2_payment_id)
            if existing and args.auto and not existing["spec"].get("automatic", False):
                raise ValueError("Начатый цикл не переводится в авторежим. Используйте --auto для нового цикла.")
            console = AutoConsole() if automatic else Console()
            mode = existing["spec"]["mode"] if existing else (args.mode or "api")
            if existing and args.mode and args.mode != mode:
                raise ValueError("Нельзя менять режим уже начатого цикла")
            if automatic and mode != "api":
                raise ValueError("--auto работает только с режимом api")
            delay_seconds, delay_max_seconds = delay_bounds(os.environ)
            members = {actor: os.getenv(f"{prefix}_MEMBER_ID", "").strip() for actor, prefix in prefixes.items()}
            nicknames = {actor: os.getenv(f"{prefix}_NICKNAME", "").strip() for actor, prefix in prefixes.items()}
            if (automatic or any(members.values())) and not nicknames["p2"]:
                raise ValueError(f"Заполните {prefixes['p2']}_NICKNAME — точный ник выбранного П2 на MEXC")
            profiles = {}
            if mode == "api":
                if not all(members.values()) or members["p1"] == members["p2"]:
                    raise ValueError(f"Для API-цикла нужны разные MEXC_P1_MEMBER_ID и {prefixes['p2']}_MEMBER_ID")
                if not settings.enable_state_changes:
                    raise ValueError("Для API-цикла установите ENABLE_STATE_CHANGES=true в .env")
                for actor in ("p1", "p2"):
                    selected = p1_profile if actor == 'p1' else p2_profile
                    profile = settings_for_profile(selected)
                    profiles[actor] = fingerprint(profile.api_key)
                    clients[actor] = MexcP2PClient(profile.api_key, profile.secret_key, profile.base_url,
                                                   profile.recv_window, proxy_url=profile.proxy_url,
                                                   timeout_seconds=45 if scheduler_mode == 'cash_volume' and reverse_maker == 'p2' else 20)
                if profiles["p1"] == profiles["p2"]:
                    raise ValueError("Для П1 и П2 указаны одинаковые API-ключи")
                if existing and profiles != existing["spec"]["profiles"]:
                    raise ValueError("API-профили изменились. Для продолжения верните ключи начатого цикла.")
            if not telegram.enabled:
                console.write("Telegram не настроен: события сохраняются локально до подключения.")
            if not sheets:
                console.write("Google Таблица не настроена: суммы продаж сохраняются локально до подключения.")
            browser = None
            maker_browser = None
            p2_payment_browser = None
            p2_payment_account_id = None
            if mode == 'api' and p2_pay_method_id:
                from adspower import AdsPower
                payment_profile_id = os.getenv(f"{prefixes['p2']}_ADSPOWER_PROFILE_ID", '').strip()
                if not payment_profile_id:
                    raise ValueError(f"{prefixes['p2']}_ADSPOWER_PROFILE_ID нужен для выбора реквизитов по payMethod")
                if existing and existing['spec'].get('p2_payment_ads_profile_id') not in {None, payment_profile_id}:
                    raise ValueError('Профиль AdsPower П2 изменился; верните настройки начатого цикла')
                p2_payment_browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                                              os.getenv('ADSPOWER_API_KEY', ''), payment_profile_id)
            if mode == "api" and os.getenv("SELLER_CHECK_MODE", "manual").strip() == "adspower":
                from adspower import AdsPower
                browser = (AdsPower.from_env() if p1_profile == 'p1' else
                           AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                                    os.getenv('ADSPOWER_API_KEY', ''),
                                    os.getenv(f"{prefixes['p1']}_ADSPOWER_PROFILE_ID", '')))
                if reverse_maker == 'p2' or (scheduler_mode == 'cash_volume' and cash_route_setting == 'p2p'):
                    maker_browser = AdsPower(os.getenv('ADSPOWER_BASE_URL', 'http://127.0.0.1:50325'),
                                             os.getenv('ADSPOWER_API_KEY', ''),
                                             os.getenv(f"{prefixes['p2']}_ADSPOWER_PROFILE_ID", ''))
                if scheduler_mode == 'cash_volume' and reverse_maker == 'p2':
                    browser.command_timeout = 30
                    maker_browser.command_timeout = 30
            browser_ids = ({'p1': browser.profile_id, 'p2': maker_browser.profile_id if maker_browser else ''}
                           if browser else {})
            if existing and existing['spec'].get('adspower_profiles') and (
                    existing['spec']['adspower_profiles'] != browser_ids):
                raise ValueError('AdsPower-профили участников изменились; продолжение остановлено')
            if automatic:
                from phrases import PHRASES
                if not browser:
                    raise ValueError("Для авторежима установите SELLER_CHECK_MODE=adspower")
                if not all(members.values()) or members["p1"] == members["p2"]:
                    raise ValueError("Для авторежима нужны разные MEXC_P1_MEMBER_ID и MEXC_P2_MEMBER_ID")
                if not os.getenv(f"{prefixes['p1']}_SELL_ADV_NO"):
                    raise ValueError(f"Заполните {prefixes['p1']}_SELL_ADV_NO")
                if reverse_maker == 'p1' and (not os.getenv(f"{prefixes['p1']}_BUY_ADV_NO") or not (p2_payment_id or p2_pay_method_id)):
                    raise ValueError(f"Для обратного ордера нужны {prefixes['p1']}_BUY_ADV_NO и способ оплаты П2")
                if reverse_maker == 'p2' and (not os.getenv(f"{prefixes['p2']}_SELL_ADV_NO")
                                               or not maker_browser or not maker_browser.profile_id):
                    raise ValueError(f"Для нового маршрута заполните {prefixes['p2']}_SELL_ADV_NO и {prefixes['p2']}_ADSPOWER_PROFILE_ID")
                if scheduler_mode == 'cash_volume' and not os.getenv(f"{prefixes['p1']}_BUY_ADV_NO"):
                    raise ValueError('Для «Объём наличка» нужно объявление покупки П1')
                if scheduler_mode == 'cash_volume' and cash_route_setting == 'p2p' and (
                      not os.getenv(f"{prefixes['p2']}_SELL_ADV_NO")
                      or not maker_browser or not maker_browser.profile_id):
                    raise ValueError('Для P2P-возврата нужны объявление продажи П2 и AdsPower П2')
                for key in ("forward_message", "forward_reply", "reverse_message", "reverse_reply"):
                    if not PHRASES.get(key) or any(not isinstance(t, str) or not t.strip() or len(t) > 2000 for t in PHRASES[key]):
                        raise ValueError(f"Проверьте список фраз {key} в phrases.py")
                await browser.ensure_started()
                await browser.ensure_mexc_page()
                async with browser.connection() as call:
                    await call("Target.getTargets")
                if p2_payment_browser and reverse_maker == 'p1' and not existing:
                    # Check the receiving account before the first order can lock USDT.
                    p2_payment_account_id = await p2_payment_browser.payment_account_by_method(
                        positive_id(p2_pay_method_id), plan['fiat'] if plan else existing['spec']['fiat'])
                if maker_browser and reverse_maker == 'p2':
                    await maker_browser.ensure_started()
                    await maker_browser.ensure_mexc_page()
                    async with maker_browser.connection() as call:
                        await call('Target.getTargets')
            cash_wait_min, cash_wait_max = 75.0, 105.0
            if scheduler_mode == 'cash_volume' and not existing:
                cash_wait_min = float(os.getenv('CASH_REVERSE_WAIT_MIN_SECONDS', '75'))
                cash_wait_max = float(os.getenv('CASH_REVERSE_WAIT_MAX_SECONDS', '105'))
            if scheduler_mode == 'cash_volume' and not existing and (
                    not math.isfinite(cash_wait_min) or not math.isfinite(cash_wait_max)
                    or not 1 <= cash_wait_min <= cash_wait_max <= 600):
                raise ValueError('CASH_REVERSE_WAIT_MIN_SECONDS/MAX_SECONDS: укажите интервал от 1 до 600 секунд')
            eflp_wait_min, eflp_wait_max = 75.0, 105.0
            if scheduler_mode in {'eflp_volume', 'eflp_unique'} and not existing:
                eflp_wait_min = float(os.getenv('EFLP_WAIT_MIN_SECONDS', '75'))
                eflp_wait_max = float(os.getenv('EFLP_WAIT_MAX_SECONDS', '105'))
                if (not math.isfinite(eflp_wait_min) or not math.isfinite(eflp_wait_max)
                        or not 1 <= eflp_wait_min <= eflp_wait_max <= 600):
                    raise ValueError('EFLP_WAIT_MIN_SECONDS/MAX_SECONDS: укажите интервал от 1 до 600 секунд')
            eflp_phrases = None
            if scheduler_mode in {'eflp_volume', 'eflp_unique'} and not existing:
                from phrases import phrases_for_mode
                eflp_phrases = phrases_for_mode(scheduler_mode, os.environ)
            amount_input = f"{random_amount(plan)} {plan['fiat']}" if plan else (args.amount or "")
            cycle_id = args.resume or journal.create(new_spec(console, mode, profiles,
                sell_adv_no=os.getenv(f"{prefixes['p1']}_SELL_ADV_NO", ""),
                buy_adv_no=os.getenv(f"{prefixes['p2']}_SELL_ADV_NO" if reverse_maker == 'p2'
                                     else f"{prefixes['p1']}_BUY_ADV_NO", ""),
                amount_input=amount_input, automatic=automatic,
                reverse_maker=reverse_maker, p1_profile=p1_profile)
                | ({"members": members} if all(members.values()) else {})
                | ({"nicknames": nicknames} if nicknames["p2"] else {})
                | {"p2_profile": p2_profile, "p2_payment_id": p2_payment_id}
                | ({'p2_pay_method_id': p2_pay_method_id,
                    'p2_payment_ads_profile_id': p2_payment_browser.profile_id}
                   if p2_payment_browser else {})
                | ({'scheduler_mode': args.scheduler_mode} if getattr(args, 'scheduler_mode', None) else {})
                | ({'cash_route_selected': False,
                     'cash_final_return': cash_route_setting,
                    'cash_policy': 'rolling24_p1',
                    'cash_p1_buy_adv_no': os.getenv(f"{prefixes['p1']}_BUY_ADV_NO", '').strip(),
                    'cash_p2_sell_adv_no': os.getenv(f"{prefixes['p2']}_SELL_ADV_NO", '').strip(),
                    'cash_wait_min_seconds': cash_wait_min,
                    'cash_wait_max_seconds': cash_wait_max}
                   if scheduler_mode == 'cash_volume' and not existing else {})
                | ({'eflp_wait_min_seconds': eflp_wait_min,
                    'eflp_wait_max_seconds': eflp_wait_max,
                    'eflp_p1_pay_method_id': eflp_p1_pay_method_id,
                    'eflp_phrases': eflp_phrases}
                   if scheduler_mode in {'eflp_volume', 'eflp_unique'} and not existing else {})
                | ({'adspower_profiles': browser_ids} if browser_ids else {})
                | ({"series": plan} if plan else {}))
            runner = CycleRunner(journal, reporter, clients, console, state_changes=settings.enable_state_changes,
                                 p2_payment_id=p2_payment_id, p2_profile=p2_profile, browser=browser,
                                 p2_pay_method_id=p2_pay_method_id, p2_payment_browser=p2_payment_browser,
                                 p2_payment_account_id=p2_payment_account_id,
                                 p1_profile=p1_profile, maker_browser=maker_browser,
                                 pay_method_id=os.getenv("MEXC_PAY_METHOD_ID", "578"), automatic=automatic, delay_seconds=delay_seconds,
                                 trusted_members=members if all(members.values()) else {},
                                 trusted_nicknames=nicknames, delay_max_seconds=delay_max_seconds,
                                 p1_over_verify=os.getenv(f"{prefixes['p1']}_OVER_VERIFY", "").strip(), stop_event=stop_event)
            runner_started = True
            await run_series(runner, cycle_id)
            return 0
        except CashVolumeLimitReached:
            raise
        except Paused as exc:
            print(exc)
            return 0
        except Exception as exc:
            if notify_prepare_errors and not runner_started and args.command == "cycle":
                from adspower import AdsPowerError
                reason = str(exc) if isinstance(exc, (ValueError, MexcAPIError, AdsPowerError)) else type(exc).__name__
                await telegram.send("❌ Цикл не запущен: ошибка подготовки\n" + reason[:700]
                                    + "\nПроверьте настройки и вывод консоли. Торговые действия не выполнялись.")
            raise
        finally:
            for client in clients.values():
                await client.close()
            journal.close()
