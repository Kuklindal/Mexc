"""Read an order's state from both cash accounts without printing order details."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from mexc_client import MexcP2PClient  # noqa: E402


async def inspect(account: str, order_no: str, profile: str) -> None:
    settings = Settings.from_env(account, p2_profile=profile if account == 'p2' else None)
    client = MexcP2PClient(settings.api_key, settings.secret_key, settings.base_url,
                          settings.recv_window, settings.proxy_url)
    try:
        detail = await client.get_order_detail(order_no)
        print(account.upper(), 'статус', detail.get('state'),
              'USDT', detail.get('tradableQuantity') or detail.get('quantity') or '—',
              flush=True)
    except Exception as exc:
        print(account.upper(), 'ошибка', type(exc).__name__, flush=True)
    finally:
        await client.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('order_no')
    parser.add_argument('p2_profile')
    args = parser.parse_args()
    load_dotenv(os.environ.get('MEXC_ENV_FILE', str(ROOT / '.env.cash2')), override=True)
    await inspect('p1', args.order_no, args.p2_profile)
    await inspect('p2', args.order_no, args.p2_profile)


if __name__ == '__main__':
    asyncio.run(main())
