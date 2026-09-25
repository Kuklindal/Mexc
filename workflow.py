from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mexc_client import MexcP2PClient
from notifier import TelegramNotifier


class MerchantOrderMonitor:
    """
    Monitors genuine merchant orders and notifies on changes.
    It intentionally does NOT create a self-trading / reverse-trading loop.
    """

    def __init__(
        self,
        client: MexcP2PClient,
        notifier: TelegramNotifier,
        poll_interval_seconds: int = 5,
        lookback_hours: int = 24,
    ):
        self.client = client
        self.notifier = notifier
        self.poll_interval_seconds = poll_interval_seconds
        self.lookback_hours = lookback_hours
        self.logger = logging.getLogger("mexc_p2p.monitor")
        self._known_states: dict[str, str] = {}

    @staticmethod
    def _counterparty(order: dict[str, Any]) -> str:
        merchant = order.get("merchantInfo")
        user = order.get("userInfo")

        if isinstance(merchant, dict):
            return merchant.get("nickName") or merchant.get("memberId") or "merchant"
        if isinstance(user, dict):
            return user.get("nickName") or user.get("account") or "user"
        return "unknown"

    @staticmethod
    def _summary(order: dict[str, Any]) -> str:
        return (
            f"Order {order.get('advOrderNo')} | "
            f"{order.get('side')} {order.get('tradableQuantity')} {order.get('coinName')} | "
            f"{order.get('amount')} {order.get('fiatUnit')} | "
            f"state={order.get('state')} | "
            f"counterparty={MerchantOrderMonitor._counterparty(order)}"
        )

    async def run_forever(self) -> None:
        await self.notifier.send("🟢 MEXC P2P monitor started")
        self.logger.info("Monitor started")

        while True:
            try:
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - self.lookback_hours * 3600 * 1000
                orders = await self.client.list_orders(
                    start_time_ms=start_ms,
                    end_time_ms=now_ms,
                    limit=100,
                )

                for order in orders:
                    order_no = str(order.get("advOrderNo", ""))
                    if not order_no:
                        continue

                    state = str(order.get("state", "UNKNOWN"))
                    previous = self._known_states.get(order_no)

                    if previous is None:
                        self._known_states[order_no] = state
                        self.logger.info("Discovered: %s", self._summary(order))
                        await self.notifier.send(
                            "🆕 New/visible P2P order\n" + self._summary(order)
                        )
                    elif previous != state:
                        self._known_states[order_no] = state
                        self.logger.info(
                            "State changed %s: %s -> %s",
                            order_no,
                            previous,
                            state,
                        )
                        await self.notifier.send(
                            f"🔄 P2P state changed\n{order_no}: {previous} → {state}\n"
                            + self._summary(order)
                        )

                        if state == "PAID":
                            await self.notifier.send(
                                "⚠️ Buyer marked order as PAID.\n"
                                "Do NOT release crypto until the incoming bank payment "
                                "has been independently verified."
                            )

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Monitor iteration failed")
                await self.notifier.send(
                    "🔴 MEXC P2P monitor error. Check logs/mexc_p2p.log"
                )

            await asyncio.sleep(self.poll_interval_seconds)
