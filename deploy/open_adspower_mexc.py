"""Start P1 and require a rendered MEXC page inside its AdsPower profile."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adspower import AdsPower  # noqa: E402
from config import ENV_FILE  # noqa: E402
from deploy.start_adspower_profile import main as start_profile  # noqa: E402

async def main() -> None:
    load_dotenv(ENV_FILE)
    await start_profile()
    await AdsPower.from_env().ensure_mexc_page()
    print('AdsPower: страница MEXC в профиле П1 загрузила содержимое', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
