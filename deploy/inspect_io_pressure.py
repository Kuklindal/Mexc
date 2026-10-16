"""Read-only two-second sample of disk I/O by visible processes and vda."""
from __future__ import annotations

import time
from pathlib import Path


def process_io() -> dict[int, tuple[str, int, int]]:
    result = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            name = (directory / 'comm').read_text().strip()
            fields = dict(line.split(':', 1) for line in (directory / 'io').read_text().splitlines())
            result[int(directory.name)] = (name, int(fields['read_bytes']), int(fields['write_bytes']))
        except (OSError, KeyError, ValueError):
            continue
    return result


def disk_io() -> tuple[int, int, int, int, int] | None:
    try:
        values = [int(value) for value in Path('/sys/block/vda/stat').read_text().split()]
    except (OSError, ValueError):
        return None
    # Completed read/write requests, sectors read/written, and busy milliseconds.
    return values[0], values[2], values[4], values[6], values[9]


def main() -> None:
    before, disk_before = process_io(), disk_io()
    started = time.monotonic()
    time.sleep(2)
    after, disk_after = process_io(), disk_io()
    seconds = time.monotonic() - started
    rows = []
    for pid, (name, reads, writes) in after.items():
        old = before.get(pid)
        if old is None or old[0] != name:
            continue
        read_mb = max(0, reads - old[1]) / 1048576 / seconds
        write_mb = max(0, writes - old[2]) / 1048576 / seconds
        if read_mb >= 0.1 or write_mb >= 0.1:
            rows.append((read_mb + write_mb, pid, name, read_mb, write_mb))
    print(f'Окно измерения: {seconds:.1f} сек')
    if disk_before and disk_after:
        reads = disk_after[0] - disk_before[0]
        writes = disk_after[2] - disk_before[2]
        print(f'Диск vda: чтение {(disk_after[1] - disk_before[1]) * 512 / 1048576 / seconds:.1f} МБ/с, '
              f'запись {(disk_after[3] - disk_before[3]) * 512 / 1048576 / seconds:.1f} МБ/с, '
              f'операций чтения {reads}, записи {writes}, '
              f'занят {(disk_after[4] - disk_before[4]) / 10 / seconds:.0f}% времени')
    print('Процессы с дисковым вводом-выводом (доступные текущему пользователю):')
    for _, pid, name, read_mb, write_mb in sorted(rows, reverse=True)[:12]:
        print(f'  {pid} {name}: чтение {read_mb:.1f} МБ/с, запись {write_mb:.1f} МБ/с')
    if not rows:
        print('  за время выборки не найдены')


if __name__ == '__main__':
    main()
