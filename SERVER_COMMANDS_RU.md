# Команды для текущего Linux-сервера

Это памятка для установки в `/home/mexc/mexc_auto/rep` под пользователем `mexc`.
Наличка использует `.env` и службу `mexc-telegram.service`, Eflp — `.env.eflp`
и `mexc-parallel@eflp.service`. У них должны быть **разные** Telegram-токены,
`CYCLE_DB` и вкладки Google Таблицы. Все блоки ниже — команды **Bash на сервере**,
кроме явно помеченного PowerShell. Текст приглашения вида `mexc@server$` копировать
не нужно. Команды не выводят содержимое `.env`, ключи или адреса прокси.

## Перейти в проект

```bash
cd ~/mexc_auto/rep
```

Во всех следующих блоках предполагается, что вы уже выполнили эту команду.
`MEXC_ENV_FILE="$PWD/.env"` выбирает наличку; `"$PWD/.env.eflp"` выбирает Eflp.
Указание файла перед командой действует **только на эту команду**.

## Telegram-боты: запустить, остановить, проверить

```bash
# Наличка
systemctl --user start mexc-telegram.service
systemctl --user stop mexc-telegram.service
systemctl --user restart mexc-telegram.service
systemctl --user is-active mexc-telegram.service

# Eflp
systemctl --user start mexc-parallel@eflp.service
systemctl --user stop mexc-parallel@eflp.service
systemctl --user restart mexc-parallel@eflp.service
systemctl --user is-active mexc-parallel@eflp.service

# Оба сразу
systemctl --user is-active mexc-telegram.service mexc-parallel@eflp.service
```

`active` означает, что процесс бота работает, но **не** означает, что серия сейчас
совершает сделки. Этап серии смотрите кнопкой **«Статус»** в соответствующем
Telegram-боте. Остановить службу — не то же самое, что нажать «Остановить» в
Telegram: если серия работала и в службе задано `AUTO_RESUME_ON_BOOT=true`, после
нового запуска она может продолжиться. Не перезапускайте службу посреди
невыясненного ордера без проверки статуса на MEXC.

Если нужно запустить вручную в консоли **вместо** службы, сначала остановите
соответствующую службу. `Ctrl+C` завершит такой ручной процесс; закрытие SSH
терминала тоже может его остановить.

```bash
systemctl --user stop mexc-telegram.service
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py telegram-control

systemctl --user stop mexc-parallel@eflp.service
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py telegram-control
```

Команды ручного запуска приведены по отдельности: каждый процесс держит свой
терминал. Не запускайте службу и ручной процесс одного бота одновременно.

## Автозапуск после перезагрузки

```bash
systemctl --user enable --now mexc-telegram.service
systemctl --user enable --now mexc-parallel@eflp.service
systemctl --user is-enabled mexc-telegram.service mexc-parallel@eflp.service
loginctl show-user "$USER" -p Linger
```

Для запуска пользовательских служб после перезагрузки без входа по SSH нужно
`Linger=yes`. Если показано `Linger=no`, попросите администратора сервера
выполнить `sudo loginctl enable-linger mexc`. Настройки служб не подменяют
ручную сверку незавершённого цикла на MEXC.

## AdsPower и профили П1

Общий AdsPower обслуживает оба бота. Его состояние можно проверить без `sudo`:

```bash
systemctl is-active adspower-headless.service
```

Если служба AdsPower неактивна, её перезапуск требует прав администратора:
`sudo systemctl restart adspower-headless.service`. Не перезапускайте общий
AdsPower во время активного действия одного из ботов.

Наличка — открыть П1 и вкладку MEXC:

```bash
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py adspower-open
```

Наличка — закрыть П1. Если не хотите, чтобы бот снова открыл профиль, сначала
остановите его службу:

```bash
systemctl --user stop mexc-telegram.service
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python deploy/stop_adspower_profile.py
```

Eflp — открыть или закрыть **ровно один** профиль по его AdsPower ID. Ниже пример
для `p1_3`; для другого П1 замените `MEXC_P1_3_ADSPOWER_PROFILE_ID` на его поле
из `.env.eflp`.

```bash
PROFILE_ID=$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env.eflp")["MEXC_P1_3_ADSPOWER_PROFILE_ID"])')
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py browser-open "$PROFILE_ID"
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py browser-close "$PROFILE_ID"
```

`browser-open` открывает профиль, но **не проверяет содержимое вкладки MEXC**.
Для открытия профиля `p1_3` вместе со страницей MEXC используйте:

```bash
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python - <<'PY'
import asyncio
import os
from adspower import AdsPower
from config import ENV_FILE

profile = os.getenv("MEXC_P1_2_ADSPOWER_PROFILE_ID", "").strip()
if not profile:
    raise SystemExit(f"Нет MEXC_P1_2_ADSPOWER_PROFILE_ID в {ENV_FILE}")
browser = AdsPower(os.getenv("ADSPOWER_BASE_URL", "http://127.0.0.1:50325"),
                   os.getenv("ADSPOWER_API_KEY", ""), profile)

async def main():
    await browser.ensure_started()
    await browser.ensure_mexc_page()
    print("П1 Eflp и страница MEXC готовы")

asyncio.run(main())
PY
```

Проверить кнопку «Проверка пройдена» у **существующего** наличного ордера без
нажатия кнопки:

```bash
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py adspower-check НОМЕР_ОРДЕРА
```

В Eflp этот шаг не используется. Браузер П2 не нужен для обычного запуска Eflp,
если его ID реквизитов уже настроен. Не закрывайте профиль П1, пока бот совершает
действие с объявлением или ордером.

## Проверить объявления, реквизиты и ордер без сделки

Эти команды только читают данные MEXC. Подставьте свой номер ордера и профиль:

```bash
# Объявление продажи выбранного П1 Eflp: показать payMethod и paymentInfo.id
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py ad-payments --p1-profile p1_3

# Реквизиты П2 по общему payMethod; нужен профиль AdsPower этого П2
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py payment-method-check --p2-profile 12 --fiat GEL --mode eflp

# Вывести ID реквизитов выбранных П2 через их профили AdsPower
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py payment-ids --profiles 2,3,12 --mode eflp --fiat GEL

# Сверить конкретный ордер через API выбранного участника
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py detail НОМЕР_ОРДЕРА --account p1
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py detail НОМЕР_ОРДЕРА --account p2 --p2-profile 6

# Список всех подкоманд и параметров
.venv/bin/python main.py --help
```

`payment-method-check` и `payment-ids` работают только для П2 с настроенным
AdsPower-профилем. Команды `create-order`, `mark-paid`, `release`, `chat` и
`wallet-transfer` совершают реальные действия; их параметры можно посмотреть
через `.venv/bin/python main.py ИМЯ_КОМАНДЫ --help`.

## Логи

```bash
# Последние 50 строк налички / Eflp
journalctl --user -u mexc-telegram.service -n 50 --no-pager
journalctl --user -u mexc-parallel@eflp.service -n 50 --no-pager

# За последний час
journalctl --user -u mexc-telegram.service --since '1 hour ago' --no-pager
journalctl --user -u mexc-parallel@eflp.service --since '1 hour ago' --no-pager

# Следить за новыми строками; выйти — Ctrl+C
journalctl --user -fu mexc-telegram.service
journalctl --user -fu mexc-parallel@eflp.service
```

Логи общей службы AdsPower в отдельном пространстве `mexc`. Если у вашего
пользователя есть доступ к этому журналу:

```bash
journalctl --namespace=mexc -u adspower-headless.service -n 50 --no-pager
```

Если появится `No entries` или отказ в доступе, администратор может выполнить
ту же команду с `sudo`. Это **технические логи**. Долговечный журнал циклов —
SQLite-файл из `CYCLE_DB`; его нельзя удалять вместе с логами.

## Циклы, журнал и Google Таблица

Пока бот работает как служба, текущий этап смотрите через Telegram **«Статус»**.
Команды `cycle-status` и `sync-journal` используют блокировку того же журнала;
для их запуска остановите **только соответствующую** службу и после проверки
запустите её снова:

```bash
# Наличка
systemctl --user stop mexc-telegram.service
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py cycle-status
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py sync-journal
systemctl --user start mexc-telegram.service

# Eflp
systemctl --user stop mexc-parallel@eflp.service
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py cycle-status
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py sync-journal
systemctl --user start mexc-parallel@eflp.service
```

`sync-journal` доставляет отложенные записи, но не переносит отсутствующие сделки
из другого журнала. Перед переносом или восстановлением SQLite сравните базы.

Для продолжения **уже сверенного** остановленного цикла без Telegram запустите
команду ниже только при остановленной службе соответствующего бота:

```bash
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py cycle --resume ID_ЦИКЛА
MEXC_ENV_FILE="$PWD/.env.eflp" .venv/bin/python main.py cycle --resume ID_ЦИКЛА
```

Эти строки — альтернативы для разных журналов, не последовательные шаги.
`--resume` может отправлять новые действия на MEXC. Если первая сделка завершена,
а обратная не создана, сначала сверяйте USDT и историю ордеров обоих аккаунтов.
Изменение `.env` не меняет параметры, уже сохранённые в цикле.

Сбросить **только местную запись** цикла можно после сверки MEXC и при
остановленной службе. Команда попросит ввести подтверждение; ордера биржи и
балансы она не меняет:

```bash
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python main.py cycle-reset ID_ЦИКЛА
# Для Eflp вместо .env укажите .env.eflp
```

## Резервная копия SQLite без остановки бота

SQLite `backup()` создаёт согласованную копию работающей базы. Выберите файл
настроек в первой части команды; повторите её отдельно для Eflp.

```bash
MEXC_ENV_FILE="$PWD/.env" .venv/bin/python - <<'PY'
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values

env_file = Path(os.environ["MEXC_ENV_FILE"])
env = dotenv_values(env_file)
source = Path(env.get("CYCLE_DB") or "data/cycles.sqlite3")
if not source.is_absolute():
    source = Path.cwd() / source
if not source.is_file():
    raise SystemExit(f"Журнал не найден: {source}")
target = Path("backups") / f"{env_file.name.lstrip('.')}-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
target.parent.mkdir(exist_ok=True)
with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
print("Копия:", target.resolve())
PY
```

Для Eflp замените `.env` на `.env.eflp`. Для передачи готовой копии на Windows
запустите **в PowerShell на компьютере** (подставьте IP и имя файла из вывода):

```powershell
scp mexc@SERVER_IP:~/mexc_auto/rep/backups/ИМЯ_КОПИИ.sqlite3 .
```

Не копируйте работающий `cycles.sqlite3` обычным `scp`: изменения могут
находиться в его WAL-файле. Пароли, API-ключи и `.env` в команды диагностики
или сообщения с логами не вставляйте.

## Проверка сервера

```bash
free -h
df -h /
pgrep -c SunBrowser || true
systemctl is-active adspower-headless.service
systemctl --user is-active mexc-telegram.service mexc-parallel@eflp.service
systemctl --user is-enabled mexc-telegram.service mexc-parallel@eflp.service
loginctl show-user "$USER" -p Linger
```

Проверка MEXC-прокси делается отдельно для нужного профиля; не печатайте URL
прокси из `.env`, так как в нём могут быть логин и пароль.

## Обновление кода с Windows без перезаписи настроек и журнала

Сначала остановите серии через Telegram, дождитесь завершения текущего действия,
сверьте незавершённые ордера и сделайте резервные копии обоих журналов.
На **Windows в PowerShell** из каталога проекта:

```powershell
tar -czf "$env:TEMP\mexc-code.tar.gz" --exclude=.venv --exclude=.git --exclude=.env '--exclude=.env.*' --exclude=data --exclude=logs --exclude=credentials --exclude=backups .
scp "$env:TEMP\mexc-code.tar.gz" mexc@SERVER_IP:~/mexc-code.tar.gz
```

На сервере:

```bash
cd ~/mexc_auto/rep
systemctl --user stop mexc-telegram.service mexc-parallel@eflp.service
tar -xzf ~/mexc-code.tar.gz
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q
systemctl --user start mexc-telegram.service mexc-parallel@eflp.service
```

Технические логи `mexc` настроены на хранение до двух суток; база `CYCLE_DB`
с историей сделок и таймерами этим правилом не удаляется.
