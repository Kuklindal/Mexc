"""One operator-controlled, resumable two-leg USDT workflow."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from decimal import Decimal, InvalidOperation
from getpass import getpass
import hashlib
import json
import random
import uuid
from typing import Callable

from journal import Journal
from mexc_client import MexcAPIError, ad_replenish_params, ad_verification, counterparty_identity
from sheets import Reporter


COMPLETED_STATES = {"DONE", "COMPLETED"}
PAID_STATES = {"PAID"} | COMPLETED_STATES
# Reverse taker SELL orders can show PROCESSING while the buyer is asked to pay.
# This permits a confirmed mark-paid request, never release or paid reconciliation.
PAYMENT_START_STATES = {"forward": {"NOT_PAID"}, "reverse": {"NOT_PAID", "PROCESSING"}}


class Paused(RuntimeError):
    pass


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
    Step("reverse_replenish", "p1", "П1 пополняет исходное объявление продажи полученными USDT"),
]


def fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def new_spec(console: Console, mode: str, profiles: dict[str, str], *,
             sell_adv_no: str = "", buy_adv_no: str = "", amount_input: str = "", automatic: bool = False) -> dict:
    amount, fiat = purchase_amount(amount_input) if amount_input else console.ask(
        "Сумма покупки П2 (например 1000 RUB; без валюты — RUB)", validate=purchase_amount)
    console.write(f"П2 купит у П1 USDT на {amount} {fiat}.")
    adv_no = sell_adv_no.strip() or console.ask("Номер готового объявления П1 о продаже USDT (advNo)")
    return {"mode": mode, "amount": amount, "fiat": fiat, "profiles": profiles,
            "forward_adv_no": adv_no, "reverse_adv_no": buy_adv_no.strip(), "automatic": automatic}


def auto_plan(args, env: dict) -> dict:
    """Validate the whole batch before creating its first cycle."""
    count = args.count if args.count is not None else int(env.get("AUTO_CYCLE_COUNT", "1"))
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
            if runner.automatic and series and series["index"] == series["count"]:
                runner.console.write(f"Серия завершена: {series['count']} циклов.")
            return
        if (runner.journal.cycle(cycle_id)["status"] != "completed"
                or not runner.result("reverse_replenish")):
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
                 pay_method_id: str = "", automatic: bool = False, delay_seconds: float = 20,
                 trusted_members: dict | None = None, p1_over_verify: str = "",
                 trusted_nicknames: dict | None = None, delay_max_seconds: float | None = None,
                 p2_profile: str = "default", stop_event: asyncio.Event | None = None):
        self.journal = journal
        self.reporter = reporter
        self.clients = clients
        self.console = console
        self.state_changes = state_changes
        self.p2_payment_id = p2_payment_id.strip()
        self.p2_profile = p2_profile
        self.browser = browser
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
        if "p2_payment_id" in self.spec and self.spec["p2_payment_id"] != self.p2_payment_id:
            raise ValueError("Реквизиты П2 изменились; верните настройки начатого цикла")
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
        if cycle["status"] == "completed" and self.result("reverse_replenish"):
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
            for step in STEPS:
                current = step
                self.check_stop()
                saved = self.journal.step(cycle_id, step.key)
                if (step.key == 'reverse_create' and saved and saved['status'] == 'unknown'
                        and not saved['result'].get('order_no') and self.journal.reverse_daily_limit_rejected(cycle_id)):
                    self.journal.transition(cycle_id, step.key, step.actor, 'rejected',
                        'Восстановлен явный отказ MEXC 60085 из журнала последней попытки',
                        result={'rejected_code': 60085}, context=self.context(step.key))
                    saved = self.journal.step(cycle_id, step.key)
                if saved and saved['status'] == 'rejected' and saved['result'].get('rejected_code') == 60085:
                    if self.automatic:
                        raise Paused(self.daily_limit_message())
                    self.console.confirm(
                        self.daily_limit_message() + "\nПодтвердите, что доступный лимит проверен на MEXC и позволяет эту сделку.",
                        f"ЛИМИТ ПРОВЕРЕН {step.key}")
                    saved = None
                leg, action = step.key.split("_", 1)
                remote = None
                if (step.key == "forward_check" and saved and saved["status"] == "done"
                        and self.browser and self.spec["mode"] == "api"
                        and not self.result("forward_paid")):
                    fresh = await self.snapshot(leg)
                    if fresh["state"] == "NOT_PAID" and await self.browser.inspect(fresh["order_no"]) not in {"passed", "not_required"}:
                        await self.event(step, "pending", "Проверка документов не подтверждена; прежняя отметка по заголовку отменена")
                        saved = None
                if self.spec["mode"] == "api" and action in {"paid", "release"}:
                    remote = await self.snapshot(leg)
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
                    continue
                await self.event(step, "waiting", step.label, cycle_status="active" if action == "replenish" else None)
                self.console.write(f"\n[{step.key}] {step.label}")
                uncertain = bool(saved and saved["status"] in {"in_flight", "unknown"})
                retry = False
                already_applied = remote is not None and remote["state"] in (PAID_STATES if action == "paid" else COMPLETED_STATES)
                if already_applied:
                    uncertain = True
                if self.automatic and uncertain and not already_applied:
                    raise Paused("Результат предыдущей операции требует сверки. Автоматического повтора нет; продолжите с --interactive.")
                if uncertain and action == "replenish" and saved["result"].get("rejected_code") in {700002, 60048, 60064}:
                    plan = {k: v for k, v in saved["result"].items() if k != "rejected_code"}
                    ad = await self.clients["p1"].get_ad(plan["adv_no"])
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
                if self.automatic and action in MUTATING_ACTIONS and not uncertain:
                    remaining = random.uniform(self.delay_seconds, self.delay_max_seconds)
                    self.console.write(f"Пауза {remaining:.2f} сек. перед действием.")
                    while remaining > 0:
                        await self.wait_delay(min(5, remaining))
                        remaining -= min(5, remaining)
                        await self.guard_participants()
                self.check_stop()
                prepared = await self.prepare(step, recovery=uncertain)
                if uncertain:
                    self.console.confirm("Подтверждаю, что результат этого шага проверен на MEXC.", f"СВЕРЕНО {step.key}")
                else:
                    self.console.confirm(step.label + "\n" + json.dumps(
                        {k: v for k, v in prepared.items() if k not in {"notify_code", "settings_fingerprint"}}, ensure_ascii=False, indent=2),
                        f"ПОВТОРИТЬ {step.key}" if retry else "ДА")
                # Durable intent is committed before any mutating network request.
                self.check_stop()
                payment_context = ({"payment_account_id": prepared["payment_account_id"]}
                                   if "payment_account_id" in prepared else None)
                if action == "replenish":
                    payment_context = prepared
                await self.event(step, "in_flight", step.label + (": авторежим" if self.automatic else ": подтверждено оператором"), payment_context)
                try:
                    result = await self.execute(step, prepared, recovery=uncertain)
                except BaseException as exc:
                    if (action == 'create' and isinstance(exc, MexcAPIError)
                            and exc.code == 60085 and exc.http_status in {200, 400}):
                        self.journal.transition(cycle_id, step.key, step.actor, 'rejected',
                            'Создание ордера отклонено: дневной лимит MEXC 60085',
                            result=dict(prepared, rejected_code=60085), context=self.context(step.key))
                        raise Paused(self.daily_limit_message()) from exc
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
            await self.event(Step("cycle", "both", "Цикл"), "completed", "Цикл завершён", cycle_status="completed")
            await self.close_completed_tabs()
            self.console.write("Цикл завершён.")
        except BaseException as exc:
            status = ("stopped" if isinstance(exc, (OperatorStopped, KeyboardInterrupt)) else
                      "paused" if isinstance(exc, (Paused, KeyboardInterrupt)) else "error")
            reason = str(exc) if isinstance(exc, (Paused, MexcAPIError, ValueError)) else type(exc).__name__
            from adspower import AdsPowerError
            if isinstance(exc, AdsPowerError):
                reason = str(exc)
            self.journal.transition(cycle_id, current.key, current.actor, status,
                f"{current.label}: {reason[:700]}",
                context=self.context(current.key), cycle_status="paused")
            if isinstance(exc, Exception):
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

    async def close_completed_tabs(self):
        if not self.browser or self.spec["mode"] != "api":
            return
        if not all(self.result(key) for key in ("forward_complete", "reverse_complete", "reverse_replenish")):
            return
        try:
            closed = await self.browser.close_order_tabs([
                self.result(f"{leg}_create")["order_no"] for leg in ("forward", "reverse")])
            self.console.write(f"Закрыто вкладок завершённых ордеров: {closed}.")
        except Exception as exc:
            # Trading is already completed; tab cleanup must not cause a replay.
            self.console.write(f"Цикл завершён, но вкладки закрыть не удалось ({type(exc).__name__}).")

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

    async def snapshot(self, leg: str) -> dict:
        order_no = self.result(f"{leg}_create")["order_no"]
        if self.spec["mode"] == "api":
            details = {}
            for actor in ("p1", "p2"):
                detail = await self.clients[actor].get_order_detail(order_no)
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
        if leg == "reverse" and Decimal(snapshot["quantity"]) != Decimal(self.result("forward_complete")["quantity"]):
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
        if len(ids) == 1:
            self.console.write(f"ID платёжных реквизитов получен из ордера: {ids[0]}")
            return ids[0]
        if self.automatic:
            selected = [ids[i] for i, payment in enumerate(payments) if str(payment.get("payMethod")) == str(self.pay_method_id)]
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
        manual = self.spec["mode"] == "manual" or recovery
        ctx = self.context(step.key)
        if action == "replenish":
            snapshot = await self.snapshot("reverse")
            self.check_state(snapshot, COMPLETED_STATES)
            quantity = money(self.result("reverse_complete")["quantity"])
            adv_no = self.result("forward_ad")["adv_no"]
            if self.spec["mode"] == "manual":
                self.console.confirm(f"На MEXC добавьте {quantity} USDT к остатку объявления {adv_no}. "
                                     "Если уже добавили, повторно не пополняйте.", f"ПОПОЛНЕНО {adv_no}")
                return {"adv_no": adv_no, "quantity": quantity}
            ad = await self.clients["p1"].get_ad(adv_no)
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
                    await self.check_browser_ad(saved)
                self.console.write(f"Доступный остаток объявления соответствует цели {saved['target_available']} USDT. "
                                   "Проверьте пополнение на MEXC; повторного запроса не будет.")
                return saved
            plan = await self.quantity_plan(ad, adv_no, quantity) if ad.get('overVerify') is None else self.replenish_plan(ad, adv_no, quantity)
            self.console.write(f"Объявление {adv_no}: сейчас {plan['before_available']} USDT, добавить {quantity} USDT. "
                               f"Ожидаемый доступный остаток: {plan['target_available']} USDT.\n"
                               f"Статус объявления: {ad.get('advStatus', 'неизвестен')}; публикация этим шагом не выполняется.")
            if "target_max_limit" in plan:
                self.console.write(f"Максимум одной сделки будет уменьшен: {plan['before_max_limit']} → "
                                   f"{plan['target_max_limit']} {self.spec['fiat']}, по стоимости объёма после пополнения. "
                                   "Подтверждение ниже разрешает и пополнение, и изменение максимума. Минимальный лимит сохраняется.")
            return plan
        if action == "ad":
            direction = "ПРОДАЖА USDT: П1 — продавец" if leg == "forward" else "ПОКУПКА USDT: П1 — покупатель"
            self.console.write(f"Используем готовое объявление П1. {direction}.\n"
                               "Проверьте владельца, валюту, цену, лимиты, доступный остаток и способ оплаты.")
            adv_no = self.spec.get(f"{leg}_adv_no")
            if not adv_no:
                adv_no = self.console.ask("Номер готового объявления П1 (advNo)")
            if self.automatic:
                ad = await self.clients["p1"].get_ad(adv_no)
                if (ad.get("advNo") != adv_no or ad.get("side") != ("SELL" if leg == "forward" else "BUY")
                        or ad.get("coinName") != "USDT" or ad.get("fiatUnit") != self.spec["fiat"]):
                    raise Paused("Объявление П1 не соответствует стороне сделки, токену или валюте")
                if leg == "forward":
                    # A configured fallback is not evidence that the live checkbox is on.
                    actual = await self.browser.ad_details(adv_no) if ad.get('overVerify') is None else ad
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
                self.console.write(f"После подтверждения П2 откроет сделку по объявлению {args['adv_no']} "
                                   f"на {self.spec['amount']} {self.spec['fiat']}.")
                args.update(amount=self.spec["amount"], user_confirm_pay_method_id=self.pay_method_id or self.console.ask(
                    "ID способа оплаты из объявления (не номер карты)", validate=positive_id))
            else:
                if self.p2_payment_id:
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
                text = random.choice(PHRASES[step.key])
            else:
                text = self.console.ask("Текст сообщения", "Здравствуйте! Готов к сделке." if action == "message" else "Здравствуйте! Вижу ваш ордер.")
            if len(text) > 2000:
                raise ValueError("Сообщение длиннее 2000 символов")
            if manual:
                self.console.write(f"Отправьте этот текст в чат ордера {ctx['order_no']} от имени {step.actor}.")
            return {"order_no": ctx["order_no"], "text": text}
        if action == "check":
            if self.browser and self.spec["mode"] == "api" and recovery:
                if await self.browser.inspect(ctx["order_no"]) not in {"passed", "not_required"}:
                    raise Paused("Предыдущее нажатие не подтверждено страницей MEXC. Завершите проверку на сайте; повторного нажатия не будет.")
            if self.browser and not manual:
                self.check_state(await self.snapshot(leg), {"NOT_PAID"})
                state = await self.browser.open_order(ctx["order_no"])
                if state == "not_required":
                    self.console.write("MEXC явно сообщает: дополнительная проверка для этого ордера не требуется.")
                    return {"order_no": ctx['order_no'], "verification": "not_required"}
                self.console.confirm(f"Проверьте документы по ордеру {ctx['order_no']}. "
                    "После подтверждения бот нажмёт «Проверка пройдена» в профиле П1 AdsPower.\n"
                    + ("На странице уже показано ожидание оплаты; повторного нажатия не будет." if state == "passed" else ""),
                    f"ДОКУМЕНТЫ ПРОВЕРЕНЫ {ctx['order_no']}")
                return {"order_no": ctx["order_no"], "verification": "adspower"}
            self.console.confirm(f"В аккаунте П1 выполните проверку продавца по ордеру {ctx['order_no']} "
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
        if self.spec["mode"] == "manual":
            return data
        if action in {"message", "reply", "check", "paid", "release", "complete"}:
            if data.get("order_no") != self.result(f"{leg}_create").get("order_no"):
                raise Paused("Номер ордера действия не совпадает с сохранённым ордером цикла")
        if self.automatic and action in MUTATING_ACTIONS and not recovery:
            await self.guard_participants()
            if action in {"message", "reply"}:
                await self.snapshot(leg)
        if action == "replenish":
            self.check_state(await self.snapshot("reverse"), COMPLETED_STATES)
            ad = await self.clients["p1"].get_ad(data["adv_no"])
            if recovery:
                self.check_replenished(ad, data)
                if data.get('method') == 'quantity_only':
                    await self.check_browser_ad(data)
                return data
            if data.get('method') == 'quantity_only':
                if await self.quantity_plan(ad, data['adv_no'], data['quantity']) != data:
                    raise Paused("Объявление изменилось после подтверждения; пополнение не отправлено")
                await self.browser.replenish_ad(data)
                self.check_replenished(await self.clients['p1'].get_ad(data['adv_no']), data)
                await self.check_browser_ad(data)
                return data
            if self.replenish_plan(ad, data["adv_no"], data["quantity"]) != data:
                raise Paused("Объявление изменилось после подтверждения. Пополнение не отправлено; проверьте его на MEXC.")
            await self.clients["p1"].replenish_ad(ad | {"overVerify": data["over_verify"]}, data["quantity"])
            self.check_replenished(await self.clients["p1"].get_ad(data["adv_no"]), data)
            return data
        # Recheck after console input: the order may have changed while the operator was reading.
        if action in {"paid", "release", "complete"}:
            expected = (PAYMENT_START_STATES[leg] if action == "paid" else {"PAID"})
            if recovery or action == "complete":
                expected = PAID_STATES if action == "paid" else COMPLETED_STATES
            self.check_state(await self.snapshot(leg), expected)
        if recovery and action == "check" and self.browser:
            if await self.browser.inspect(data["order_no"]) not in {"passed", "not_required"}:
                raise Paused("Страница MEXC не подтверждает завершение проверки документов")
        if recovery:
            return data
        if action == "check" and data.get("verification") == "not_required":
            if await self.browser.inspect(data['order_no']) != 'not_required':
                raise Paused("Требование проверки изменилось; продолжение остановлено")
            return data
        if action == "check" and data.get("verification") == "adspower":
            self.check_state(await self.snapshot(leg), {"NOT_PAID"})
            await self.browser.approve(data["order_no"])
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
            if leg == "forward" and self.browser and await self.browser.inspect(data["order_no"]) not in {"passed", "not_required"}:
                raise Paused("Дополнительная проверка документов ещё не пройдена на сервере MEXC; отметка оплаты не отправлена")
            await client.mark_paid(data["order_no"], data["payment_account_id"])
            self.check_state(await self.snapshot(leg), PAID_STATES)
        elif action == "release":
            await client.release_coin(data["order_no"], notify_type=data.get("notify_type"), notify_code=data.get("notify_code"))
            self.check_state(await self.snapshot(leg), COMPLETED_STATES)
        return {k: v for k, v in data.items() if k != "notify_code"}

    async def check_browser_ad(self, plan: dict):
        if not self.browser:
            raise Paused("Для пополнения без сброса настроек нужен открытый профиль П1 AdsPower")
        actual = await self.browser.ad_details(plan['adv_no'])
        if (str(actual.get('id')) != plan['adv_no']
                or ad_verification(actual) != plan['over_verify']
                or Decimal(str(actual.get('availableQuantity', 'NaN'))) != Decimal(plan['target_available'])):
            raise Paused("Остаток или дополнительная проверка не совпадают с планом; повторное пополнение запрещено")

    async def quantity_plan(self, ad: dict, adv_no: str, quantity: str) -> dict:
        if not self.browser:
            raise Paused("API не возвращает настройки проверки. Для пополнения без их сброса нужен AdsPower П1")
        actual = await self.browser.ad_details(adv_no)
        if (actual.get('id') != adv_no or actual.get('coinName') != 'USDT' or actual.get('tradeType') != 1
                or actual.get('currency') != self.spec['fiat'] or ad.get('advNo') != adv_no
                or Decimal(str(actual.get('availableQuantity', 'NaN'))) != Decimal(str(ad.get('availableQuantity', 'NaN')))):
            raise Paused("Данные объявления в API и браузере расходятся; пополнение остановлено")
        available = Decimal(str(actual['availableQuantity']))
        increment = Decimal(money(quantity))
        if not available.is_finite() or available < 0:
            raise ValueError("Некорректный остаток объявления")
        return {'method': 'quantity_only', 'adv_no': adv_no, 'fiat': self.spec['fiat'], 'quantity': str(increment),
                'before_available': str(available), 'target_available': str(available + increment),
                'over_verify': ad_verification(actual)}

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
            if ad_verification(ad) != plan["over_verify"]:
                raise Paused("Настройка дополнительной проверки после пополнения изменилась; повтор пополнения запрещён. Проверьте объявление на MEXC")
        if (ad.get("advNo") != plan.get("adv_no") or ad.get("side") != "SELL"
                or ad.get("coinName") != "USDT" or ad.get("fiatUnit") != self.spec["fiat"]
                or "target_available" not in plan
                or Decimal(str(ad.get("availableQuantity", "NaN"))) != Decimal(plan["target_available"])
                or ("target_max_limit" in plan and Decimal(str(ad.get("maxSingleTransAmount", "NaN"))) != Decimal(plan["target_max_limit"]))):
            raise Paused("Доступный остаток или максимальный лимит объявления не совпадает с подтверждённым планом. Автоматического повтора не будет: "
                         "сверьте пополнение и новые сделки на MEXC. Не добавляйте USDT повторно, если пополнение уже выполнено.")


async def run_command(args, *, stop_event: asyncio.Event | None = None, use_lock: bool = True,
                      notify_prepare_errors: bool = True) -> int:
    import os
    from pathlib import Path
    from contextlib import nullcontext
    from config import PROJECT_DIR, Settings, p2_prefix, select_p2_profile
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
                    pending = next((s for s in STEPS if not journal.step(row['id'], s.key)
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
            reporter = Reporter(journal, telegram, sheets)
            if args.command == "sync-journal":
                pending = await reporter.flush()
                print(f"В очереди: Telegram — {pending['telegram']}, суммы продаж — {pending['sales']}.")
                return 0 if (not telegram.enabled or not pending['telegram']) and (not sheets or not pending['sales']) else 1
            existing = journal.cycle(args.resume) if args.resume else None
            p2_profile = select_p2_profile(args.p2_profile, existing["spec"] if existing else None, os.environ)
            prefixes = {"p1": "MEXC_P1", "p2": p2_prefix(p2_profile)}
            p2_payment_id = os.getenv(f"{prefixes['p2']}_PAYMENT_ID", "").strip()
            if p2_payment_id:
                positive_id(p2_payment_id)
            automatic = (existing["spec"].get("automatic", False) if existing else args.auto) and not args.interactive
            series_options = any(v is not None for v in (args.min_amount, args.max_amount, args.count, args.fiat))
            if existing and (series_options or args.amount):
                raise ValueError("При --resume используются сохранённые суммы и количество циклов; новые параметры не задавайте")
            if series_options and not automatic:
                raise ValueError("Диапазон суммы и --count доступны только с --auto")
            plan = auto_plan(args, os.environ) if automatic and not existing else None
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
                    profile = Settings.from_env(actor, p2_profile=p2_profile if actor == "p2" else None)
                    profiles[actor] = fingerprint(profile.api_key)
                    clients[actor] = MexcP2PClient(profile.api_key, profile.secret_key, profile.base_url, profile.recv_window)
                if profiles["p1"] == profiles["p2"]:
                    raise ValueError("Для П1 и П2 указаны одинаковые API-ключи")
                if existing and profiles != existing["spec"]["profiles"]:
                    raise ValueError("API-профили изменились. Для продолжения верните ключи начатого цикла.")
            if not telegram.enabled:
                console.write("Telegram не настроен: события сохраняются локально до подключения.")
            if not sheets:
                console.write("Google Таблица не настроена: суммы продаж сохраняются локально до подключения.")
            browser = None
            if mode == "api" and os.getenv("SELLER_CHECK_MODE", "manual").strip() == "adspower":
                from adspower import AdsPower
                browser = AdsPower.from_env()
            if automatic:
                from phrases import PHRASES
                if not browser:
                    raise ValueError("Для авторежима установите SELLER_CHECK_MODE=adspower")
                if not all(members.values()) or members["p1"] == members["p2"]:
                    raise ValueError("Для авторежима нужны разные MEXC_P1_MEMBER_ID и MEXC_P2_MEMBER_ID")
                if not os.getenv("MEXC_P1_SELL_ADV_NO") or not os.getenv("MEXC_P1_BUY_ADV_NO") or not p2_payment_id:
                    raise ValueError(f"Для авторежима заполните оба номера объявлений и {prefixes['p2']}_PAYMENT_ID")
                for key in ("forward_message", "forward_reply", "reverse_message", "reverse_reply"):
                    if not PHRASES.get(key) or any(not isinstance(t, str) or not t.strip() or len(t) > 2000 for t in PHRASES[key]):
                        raise ValueError(f"Проверьте список фраз {key} в phrases.py")
                async with browser.connection() as call:
                    await call("Target.getTargets")
            amount_input = f"{random_amount(plan)} {plan['fiat']}" if plan else (args.amount or "")
            cycle_id = args.resume or journal.create(new_spec(console, mode, profiles,
                sell_adv_no=os.getenv("MEXC_P1_SELL_ADV_NO", ""),
                buy_adv_no=os.getenv("MEXC_P1_BUY_ADV_NO", ""), amount_input=amount_input, automatic=automatic)
                | ({"members": members} if all(members.values()) else {})
                | ({"nicknames": nicknames} if nicknames["p2"] else {})
                | {"p2_profile": p2_profile, "p2_payment_id": p2_payment_id}
                | ({"series": plan} if plan else {}))
            runner = CycleRunner(journal, reporter, clients, console, state_changes=settings.enable_state_changes,
                                 p2_payment_id=p2_payment_id, p2_profile=p2_profile, browser=browser,
                                 pay_method_id=os.getenv("MEXC_PAY_METHOD_ID", "578"), automatic=automatic, delay_seconds=delay_seconds,
                                 trusted_members=members if all(members.values()) else {},
                                 trusted_nicknames=nicknames, delay_max_seconds=delay_max_seconds,
                                 p1_over_verify=os.getenv("MEXC_P1_OVER_VERIFY", "").strip(), stop_event=stop_event)
            runner_started = True
            await run_series(runner, cycle_id)
            return 0
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
