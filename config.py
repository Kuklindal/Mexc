from dataclasses import dataclass
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv('MEXC_ENV_FILE') or PROJECT_DIR / '.env')
if not ENV_FILE.is_absolute():
    ENV_FILE = PROJECT_DIR / ENV_FILE
if os.getenv('MEXC_ENV_FILE') and not ENV_FILE.is_file():
    raise RuntimeError(f'Файл настроек не найден: {ENV_FILE}')
load_dotenv(ENV_FILE)


def p2_profile_name(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", name):
        raise ValueError("Имя профиля П2: 1–32 латинских буквы, цифры или подчёркивания")
    return name


def p2_prefix(name: str) -> str:
    name = p2_profile_name(name)
    return "MEXC_P2" if name == "default" else f"MEXC_P2_{name.upper()}"


def p2_nickname(profile: str, saved: str | None = None, env=None) -> str:
    """Human-readable MEXC name; keep the profile key for account selection only."""
    if saved and saved.strip():
        return saved.strip()
    source = os.environ if env is None else env
    return source.get(f"{p2_prefix(profile)}_NICKNAME", "").strip() or profile


def proxy_url(value: str, field: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {'http', 'socks5'} and parsed.hostname and parsed.port
                 and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment
                 and not any(char.isspace() for char in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f'{field}: нужен URL http:// или socks5:// с адресом и портом')
    return value


def select_p2_profile(requested: str | None, saved_spec: dict | None, env) -> str:
    if saved_spec is not None:
        # Cycles created before profiles were introduced used the legacy P2 account.
        saved = p2_profile_name(saved_spec.get("p2_profile", "default"))
        if requested is not None and p2_profile_name(requested) != saved:
            raise ValueError(f"Цикл привязан к П2 {saved}; менять профиль при --resume нельзя")
        return saved
    return p2_profile_name(requested if requested is not None else env.get("MEXC_P2_PROFILE", "default"))


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret_key: str
    base_url: str
    recv_window: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    poll_interval_seconds: int
    order_lookback_hours: int
    log_level: str
    log_dir: str
    enable_state_changes: bool
    proxy_url: str | None = None

    @classmethod
    def from_env(cls, account: str | None = None, *, require_keys: bool = True,
                 p2_profile: str | None = None) -> "Settings":
        prefix = f"MEXC_{account.upper()}" if account else "MEXC"
        if p2_profile is not None and account != "p2":
            raise ValueError("Профиль П2 можно выбирать только для аккаунта p2")
        if account == "p2":
            prefix = p2_prefix(select_p2_profile(p2_profile, None, os.environ))
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        secret_key = os.getenv(f"{prefix}_SECRET_KEY", "").strip()

        if require_keys and (not api_key or not secret_key or "replace_me" in (api_key, secret_key)):
            raise RuntimeError(
                f"Заполните {prefix}_API_KEY и {prefix}_SECRET_KEY в .env."
            )

        return cls(
            api_key=api_key,
            secret_key=secret_key,
            base_url=os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/"),
            recv_window=int(os.getenv("MEXC_RECV_WINDOW", "5000")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "5")),
            order_lookback_hours=int(os.getenv("ORDER_LOOKBACK_HOURS", "24")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_dir=os.getenv("LOG_DIR", "logs"),
            enable_state_changes=_bool("ENABLE_STATE_CHANGES", False),
            proxy_url=proxy_url(os.getenv(f"{prefix}_PROXY_URL", ""), f"{prefix}_PROXY_URL"),
        )
