import logging
import httpx


class TelegramRequestError(RuntimeError):
    """A Telegram API failure with no token, URL or message content."""


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logging.getLogger("mexc_p2p.telegram")

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def request(self, method: str, payload: dict, *, timeout: float = 10):
        """Never propagate token-bearing HTTP errors to logs or Telegram."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"https://api.telegram.org/bot{self.bot_token}/{method}", json=payload)
                response.raise_for_status()
                body = response.json()
                if body.get("ok") is not True:
                    code = body.get("error_code")
                    detail = f"HTTP {code}" if type(code) is int else "rejected response"
                    if code == 409 and method == "getUpdates":
                        detail += " (another getUpdates poller may be running)"
                    raise TelegramRequestError(f"Telegram {method}: {detail}")
                return body.get("result")
        except TelegramRequestError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = ("HTTP 409 (another getUpdates poller may be running)"
                      if status == 409 and method == "getUpdates" else f"HTTP {status}")
            raise TelegramRequestError(f"Telegram {method}: {detail}") from None
        except Exception as exc:
            raise TelegramRequestError(f"Telegram {method}: {type(exc).__name__}") from None

    async def send(self, text: str, *, reply_markup: dict | None = None) -> bool:
        if not self.enabled:
            self.logger.debug("Telegram disabled: %s", text)
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            await self.request("sendMessage", payload)
            return True
        except Exception as exc:
            detail = str(exc) if isinstance(exc, TelegramRequestError) else type(exc).__name__
            self.logger.warning("Telegram notification failed: %s", detail)
            return False
