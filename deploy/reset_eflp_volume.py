"""Reset only a completed Eflp volume series after the operator handled its final transfer."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journal import Journal, process_lock  # noqa: E402
from trade_modes import reset_completed_eflp_volume  # noqa: E402


def main() -> None:
    env_file = Path(os.environ.get('MEXC_ENV_FILE') or ROOT / '.env.eflp').resolve()
    if env_file.name != '.env.eflp' or env_file.parent != ROOT:
        raise SystemExit('Укажите только конфигурацию .env.eflp')
    load_dotenv(env_file, override=True)
    result = subprocess.run(['systemctl', '--user', 'is-active',
                             'mexc-parallel@eflp.service'], capture_output=True,
                            text=True, timeout=10)
    if result.stdout.strip() not in {'inactive', 'failed'}:
        raise SystemExit('Сначала остановите mexc-parallel@eflp.service; сброс не выполнен')
    path = Path(os.environ.get('CYCLE_DB') or 'data/cycles.sqlite3')
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise SystemExit('Журнал Eflp не найден; сброс не выполнен')
    with process_lock(path.with_suffix('.lock')):
        journal = Journal(path)
        try:
            state = reset_completed_eflp_volume(journal)
        finally:
            journal.close()
    print('Объём Eflp сброшен; история сохранена; новая точка отсчёта:',
          state['eflp_volume_reset_at'])


if __name__ == '__main__':
    main()
