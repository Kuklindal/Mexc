from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from config import Settings
from logger_setup import setup_logging
from mexc_client import MexcP2PClient
from notifier import TelegramNotifier
from workflow import MerchantOrderMonitor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MEXC P2P merchant monitor / operator CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cycle = sub.add_parser("cycle", help="Цикл П1 → П2 → П1 или автоматическая серия циклов")
    cycle.add_argument("--mode", choices=["api", "manual"], help="api (по умолчанию) — подтверждения в консоли; manual — действия кнопками на сайте")
    cycle.add_argument("--resume", metavar="ID", help="Продолжить сохранённый цикл")
    cycle.add_argument("--p2-profile", metavar="NAME", help="Профиль П2 из .env; при --resume берётся сохранённый")
    cycle.add_argument("--p1-profile", metavar="NAME", help="Профиль П1: p1, p1_2 или ранее настроенный профиль П2")
    cycle.add_argument("--reverse-maker", choices=["p1", "p2"],
                       help="Владелец объявления обратной продажи USDT; для новых режимов — p2")
    cycle.add_argument('--scheduler-mode', choices=['volume', 'unique', 'cash_volume', 'eflp_volume', 'eflp_unique'],
                        help='Внутренняя привязка к сохранённому режиму Telegram')
    control = cycle.add_mutually_exclusive_group()
    control.add_argument("--auto", action="store_true", help="Автоматический режим без подтверждений в консоли")
    control.add_argument("--interactive", action="store_true", help="Продолжить с ручными подтверждениями, включая разбор ошибки авторежима")
    cycle.add_argument("--amount", help='Фиксированная сумма первой покупки, например "9500 RUB"')
    cycle.add_argument("--min-amount", help="Минимальная случайная сумма первой покупки в фиате")
    cycle.add_argument("--max-amount", help="Максимальная случайная сумма первой покупки в фиате")
    cycle.add_argument("--fiat", help="Валюта диапазона, по умолчанию RUB")
    cycle.add_argument("--count", type=int, help="Количество полных циклов (один цикл — две сделки)")
    sub.add_parser("cycle-status", help="Показать сохранённые циклы")
    reset = sub.add_parser("cycle-reset", help="Сбросить местный цикл, сохранив историю; ордера MEXC не отменяются")
    reset.add_argument("cycle_id", metavar="ID")
    sub.add_parser("sync-journal", help="Доставить отложенные уведомления и суммы продаж")
    control_bot = sub.add_parser("telegram-control", help="Кнопки запуска, остановки, статуса и статистики в Telegram")
    control_bot.add_argument("--p2-profile", metavar="NAME", help="Профиль П2 для новых запусков; иначе из .env")
    browser_check = sub.add_parser("adspower-check", help="Проверить профиль П1 и кнопку ордера без нажатия")
    browser_check.add_argument("order_no")
    sub.add_parser("adspower-open", help="Открыть профиль П1 AdsPower и вкладку MEXC без действий по ордеру")
    ad_payments = sub.add_parser("ad-payments", help="Показать ID способов оплаты из объявления П1 без создания ордера")
    ad_payments.add_argument("--p1-profile", default="p1", metavar="NAME",
                             help="Профиль П1 из .env, например p1_3")

    transfer = sub.add_parser("wallet-transfer", help="Перевести USDT между фиатным и спотовым счетами одного аккаунта")
    transfer.add_argument("--account", required=True, choices=["p1", "p2"])
    transfer.add_argument("--p2-profile", metavar="NAME", help="Профиль П2 из .env, только с --account p2")
    transfer.add_argument("--to", required=True, choices=["spot", "fiat"], help="Счёт назначения")
    transfer.add_argument("--amount", required=True, help="Количество USDT")
    transfer_status = sub.add_parser("wallet-transfer-status", help="Сверить сохранённый перевод без повторной отправки")
    transfer_status.add_argument("transfer_id", metavar="ID", help="Местный ID операции")
    transfer_recovery = transfer_status.add_mutually_exclusive_group()
    transfer_recovery.add_argument("--tran-id", help="Номер перевода MEXC из истории, если ответ на отправку потерян")
    transfer_recovery.add_argument("--not-sent", action="store_true", help="Вручную подтвердить по истории MEXC, что перевод не создан")

    sub.add_parser("monitor", help="Monitor P2P orders and notify on state changes")

    detail = sub.add_parser("detail", help="Show one P2P order")
    detail.add_argument("order_no")

    chat = sub.add_parser("chat", help="Send a text message to an order chat")
    chat.add_argument("order_no")
    chat.add_argument("message")

    paid = sub.add_parser("mark-paid", help="Mark a genuine order as paid")
    paid.add_argument("order_no")
    paid.add_argument("payment_account_id", type=int)
    paid.add_argument(
        "--payment-sent",
        required=True,
        choices=["YES"],
        help="Required acknowledgement that fiat payment was actually sent.",
    )

    release = sub.add_parser(
        "release",
        help="Release crypto only after independently verifying bank receipt",
    )
    release.add_argument("order_no")
    release.add_argument(
        "--bank-received",
        required=True,
        choices=["YES"],
        help="Required acknowledgement that funds are visible in the receiving bank account.",
    )
    release.add_argument("--notify-type", choices=["SMS", "MAIL", "GA"])
    release.add_argument("--notify-code")

    create = sub.add_parser(
        "create-order",
        help="Create a genuine taker order against a market advertisement",
    )
    create.add_argument("adv_no")
    mode = create.add_mutually_exclusive_group(required=True)
    mode.add_argument("--amount", help="Fiat amount when buying crypto")
    mode.add_argument("--quantity", help="Crypto quantity when selling crypto")
    create.add_argument("--payment-account-id", type=int)
    create.add_argument("--pay-method-id", type=int)

    for name in ("monitor", "detail", "chat", "mark-paid", "release", "create-order"):
        sub.choices[name].add_argument("--account", choices=["p1", "p2"],
                                      help="API-профиль; без параметра используются старые MEXC_API_KEY/SECRET_KEY")
        sub.choices[name].add_argument("--p2-profile", metavar="NAME", help="Профиль П2 из .env, только с --account p2")

    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    if args.command in {"wallet-transfer", "wallet-transfer-status"}:
        from wallet import run_command
        return await run_command(args)
    if args.command == "telegram-control":
        from telegram_control import serve
        return await serve(args)
    if args.command == "adspower-check":
        from adspower import AdsPower
        state = await AdsPower.from_env().inspect(args.order_no)
        print("AdsPower: нужный ордер найден; " + ("кнопка доступна." if state == "ready" else "проверка уже пройдена."))
        print("Никаких кнопок не нажато.")
        return 0
    if args.command == "adspower-open":
        from deploy.open_adspower_mexc import main as open_mexc

        await open_mexc()
        return 0
    if args.command == "ad-payments":
        from trade_profiles import profile_prefix

        profile = args.p1_profile.strip().lower()
        settings = Settings.from_env('p1', p1_profile=profile)
        adv_no = os.getenv(f'{profile_prefix(profile)}_SELL_ADV_NO', '').strip()
        if not adv_no:
            raise ValueError(f'Для {profile} не задан номер SELL-объявления')
        client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                              settings.recv_window, proxy_url=settings.proxy_url)
        try:
            ad = await client.get_ad(adv_no)
            payments = ad.get('paymentInfo')
            if not isinstance(payments, list) or not payments:
                raise RuntimeError('MEXC не вернул способы оплаты этого объявления')
            for payment in payments:
                if not isinstance(payment, dict) or not str(payment.get('payMethod', '')).isdigit():
                    raise RuntimeError('MEXC вернул неожиданный формат способа оплаты')
                print(f"payMethod={payment['payMethod']}; paymentInfo.id={payment.get('id', '—')}")
            return 0
        finally:
            await client.close()
    if args.command in {"cycle", "cycle-status", "cycle-reset", "sync-journal"}:
        from cycle import run_command
        return await run_command(args)
    settings = Settings.from_env(args.account, p2_profile=args.p2_profile)
    setup_logging(settings.log_dir, settings.log_level)
    logger = logging.getLogger("mexc_p2p.main")

    notifier = TelegramNotifier(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
    )
    client = MexcP2PClient(
        api_key=settings.api_key,
        secret_key=settings.secret_key,
        base_url=settings.base_url,
        recv_window=settings.recv_window,
        proxy_url=settings.proxy_url,
    )

    try:
        if args.command == "monitor":
            monitor = MerchantOrderMonitor(
                client,
                notifier,
                poll_interval_seconds=settings.poll_interval_seconds,
                lookback_hours=settings.order_lookback_hours,
            )
            await monitor.run_forever()
            return 0

        if args.command == "detail":
            detail = await client.get_order_detail(args.order_no)
            print(json.dumps(detail, ensure_ascii=False, indent=2))
            return 0

        if args.command in {"chat", "mark-paid", "release", "create-order"} and not settings.enable_state_changes:
            raise RuntimeError("State-changing commands are disabled. Set ENABLE_STATE_CHANGES=true in .env.")

        if args.command == "chat":
            await client.send_chat_text(args.order_no, args.message)
            logger.info("Chat message sent to order %s", args.order_no)
            await notifier.send(f"💬 Chat message sent\nOrder: {args.order_no}")
            return 0

        if args.command == "mark-paid":
            await client.mark_paid(args.order_no, args.payment_account_id)
            logger.warning("Order %s marked as paid", args.order_no)
            await notifier.send(f"💸 Order marked paid\n{args.order_no}")
            return 0

        if args.command == "release":
            # The flag is intentionally mandatory. PAID status alone is not proof of receipt.
            await client.release_coin(
                args.order_no,
                notify_type=args.notify_type,
                notify_code=args.notify_code,
            )
            logger.warning("Crypto released for order %s", args.order_no)
            await notifier.send(f"✅ Crypto released\nOrder: {args.order_no}")
            return 0

        if args.command == "create-order":
            order_no = await client.create_order(
                adv_no=args.adv_no,
                amount=args.amount,
                tradable_quantity=args.quantity,
                user_confirm_payment_id=args.payment_account_id,
                user_confirm_pay_method_id=args.pay_method_id,
            )
            logger.warning("Created P2P order %s", order_no)
            await notifier.send(f"🆕 P2P order created\n{order_no}")
            print(order_no)
            return 0

        return 1

    finally:
        await client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(130)
    except Exception as exc:
        # Foreign API exception text can include secret-bearing URLs.
        if isinstance(exc, (RuntimeError, ValueError)):
            print(f"Ошибка: {exc}")
        else:
            print(f"Ошибка: {type(exc).__name__}. Проверьте настройки и состояние цикла.")
        raise SystemExit(1)
