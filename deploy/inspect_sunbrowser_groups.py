"""Read-only process-tree summary for SunBrowser; never print command lines."""
from __future__ import annotations

import pwd
from collections import defaultdict
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def safe_origin(pid: int) -> str:
    """Describe a browser root without printing its command line or URLs."""
    directory = Path('/proc') / str(pid)
    try:
        args = [item.decode('utf-8', 'replace') for item in
                (directory / 'cmdline').read_bytes().split(b'\0') if item]
    except OSError:
        args = []
    kind = next((item.removeprefix('--type=') for item in args
                 if item.startswith('--type=')), 'browser')
    data_dir = next((item.removeprefix('--user-data-dir=') for item in args
                     if item.startswith('--user-data-dir=')), '')
    try:
        executable = (directory / 'exe').resolve().name
    except OSError:
        executable = 'неизвестен'
    ancestors = []
    current = pid
    for _ in range(5):
        try:
            status = dict(line.split(':', 1) for line in
                          (Path('/proc') / str(current) / 'status').read_text().splitlines()
                          if ':' in line)
            parent = int(status['PPid'].strip())
            if parent <= 1:
                break
            parent_dir = Path('/proc') / str(parent)
            name = (parent_dir / 'comm').read_text().strip()
            uid = parent_dir.stat().st_uid
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
            ancestors.append(f'{name}({user}, {parent})')
            current = parent
        except (OSError, ValueError, KeyError):
            break
    return (f'тип {kind} исполняемый {executable} каталог профиля '
            f'{Path(data_dir.rstrip("/")).name if data_dir else "не указан"} '
            f'предки {" → ".join(ancestors) if ancestors else "не видны"}')


def configured_ids() -> set[str]:
    result = {'k1hshm0m'}  # A previously identified unused legacy profile.
    for path in ROOT.glob('.env*'):
        if path.name != '.env' and (not path.name.startswith('.env.') or any(
                marker in path.name for marker in ('.example', '.sample', '.bak', '.backup', '.old'))):
            continue
        for key, value in dotenv_values(path).items():
            if (key == 'ADSPOWER_P1_PROFILE_ID' or key.endswith('_ADSPOWER_PROFILE_ID')) and value:
                result.add(value.strip())
    return result


def main() -> None:
    known = configured_ids()
    processes = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if (directory / 'comm').read_text().strip() != 'SunBrowser':
                continue
            status = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines()
                          if ':' in line)
            command = (directory / 'cmdline').read_bytes()
            uid = directory.stat().st_uid
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
            processes[int(directory.name)] = {
                'parent': int(status['PPid'].strip()),
                'state': status['State'].strip().split()[0],
                'rss': int(status.get('VmRSS', '0 kB').split()[0]),
                'user': user,
                'ids': {profile_id for profile_id in known if profile_id.encode() in command},
            }
        except (OSError, ValueError, KeyError):
            continue

    groups = defaultdict(list)
    for pid, info in processes.items():
        root = pid
        seen = {pid}
        while (parent := processes[root]['parent']) in processes and parent not in seen:
            root = parent
            seen.add(root)
        groups[root].append(pid)

    print(f'Процессов SunBrowser: {len(processes)}; деревьев: {len(groups)}', flush=True)
    for root, members in sorted(groups.items(), key=lambda entry: -sum(
            processes[pid]['rss'] for pid in entry[1])):
        info = processes[root]
        ids = sorted(set().union(*(processes[pid]['ids'] for pid in members)))
        try:
            parent_name = (Path('/proc') / str(info['parent']) / 'comm').read_text().strip()
        except OSError:
            parent_name = 'неизвестен'
        print(f'  корень {root} пользователь {info["user"]} состояние {info["state"]} '
              f'родитель {parent_name} процессов {len(members)} '
              f'сумма RSS {sum(processes[pid]["rss"] for pid in members) // 1024} МиБ '
              f'профиль {", ".join(ids) if ids else "ID не виден"}', flush=True)
        if not ids:
            print('    происхождение: ' + safe_origin(root), flush=True)


if __name__ == '__main__':
    main()
