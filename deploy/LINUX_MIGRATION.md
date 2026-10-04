# Перенос двух независимых экземпляров на Linux

Эта схема рассчитана на Ubuntu Desktop 24.04, пользователя `mexc`, два каталога
`/opt/mexc/a` и `/opt/mexc/b` и один AdsPower с двумя разными профилями П1.
Имена `a`, `b`, `SERVER` и пути замените на свои. Ubuntu Server без графических
компонентов не является заявленной платформой AdsPower; сначала проверьте запуск
официального Linux-клиента и браузерного профиля на выбранном сервере.

Если оба набора П2 работают с **одним** П1 и одними объявлениями, второй процесс
не нужен: добавьте П2 в `ROLLOVER_PROFILES` первого экземпляра. Два процесса
используйте только для независимых П1, объявлений и П2. Не запускайте два
процесса с одним Telegram bot token: оба читают `getUpdates` и будут конкурировать.

## 1. Подготовьте сервер и AdsPower

Установите Python 3.11+ и официальный AdsPower для Ubuntu Desktop. Сначала войдите
в AdsPower под пользователем `mexc` через GUI, откройте **оба разных профиля П1**,
войдите в MEXC в каждом и проверьте объявления. Настройте стартовую вкладку
профиля на портал ордеров MEXC. После этого закройте GUI AdsPower: его GUI и
headless режим нельзя запускать одновременно. Сохранённый вход в MEXC после
переноса Windows → Linux проверяйте заново; одна лишь копия `.env` не переносит
браузерную сессию.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip xvfb xauth libasound2t64
python3 --version
sudo useradd --create-home --shell /bin/bash mexc
sudo install -d -o mexc -g mexc -m 750 /opt/mexc
sudo -u mexc mkdir -p /opt/mexc/a /opt/mexc/b /home/mexc/upload
command -v adspower_global
```

Если пользователь `mexc` уже есть, пропустите `useradd`. Если последняя команда
не нашла бинарный файл, укажите его абсолютный путь в
`deploy/adspower-headless.service.example`. Установленный AdsPower и его
профильные данные должны принадлежать тому же пользователю, под которым
работает служба. Не открывайте порт Local API наружу: бот подключается только к
`127.0.0.1`.

## 2. Остановите Windows-экземпляр и перенесите файлы

В Telegram нажмите «Остановить», дождитесь сохранения текущего действия, затем
полностью закройте старый процесс `telegram-control`. Сверьте на MEXC все
незавершённые ордера. Не держите старый и новый процесс одновременно с одним
набором аккаунтов и объявлений.

В PowerShell на Windows (из каталога проекта):

```powershell
$project = 'C:\Users\79025\Mexc\mexc_p2p_merchant_bot'
$bundle = "$env:TEMP\mexc-code.tar.gz"
tar -czf $bundle --exclude=.venv --exclude=.git --exclude=.env --exclude=data --exclude=logs --exclude=credentials -C $project .
scp $bundle mexc@SERVER:/home/mexc/upload/
scp "$project\.env" mexc@SERVER:/home/mexc/upload/a.env
Set-Location $project
.\.venv\Scripts\python.exe -c "import sqlite3; source=sqlite3.connect('data/cycles.sqlite3'); target=sqlite3.connect('cycles-export.sqlite3'); source.backup(target); target.close(); source.close()"
scp "$project\cycles-export.sqlite3" mexc@SERVER:/home/mexc/upload/
```

Отдельно перенесите JSON сервисного аккаунта Google, если Google Sheets включён.
Например, из PowerShell: `scp "$project\credentials\google-service-account.json"
mexc@SERVER:/home/mexc/upload/a-google.json`. На сервере после создания
каталогов выполните
`sudo -u mexc cp /home/mexc/upload/a-google.json
/opt/mexc/a/credentials/google-service-account.json` и
`sudo -u mexc chmod 600 /opt/mexc/a/credentials/google-service-account.json`.
Для второго независимого экземпляра перенесите **его собственный** `.env`, файл
Google и журнал, если они уже существуют. Не копируйте журнал первого экземпляра
во второй. Если второй экземпляр новый, создайте его `.env` из `.env.example`, а
`data/cycles.sqlite3` программа создаст сама.

На сервере:

```bash
sudo -u mexc tar -xzf /home/mexc/upload/mexc-code.tar.gz -C /opt/mexc/a
sudo -u mexc tar -xzf /home/mexc/upload/mexc-code.tar.gz -C /opt/mexc/b
sudo -u mexc chmod 600 /home/mexc/upload/a.env
sudo -u mexc cp /home/mexc/upload/a.env /opt/mexc/a/.env
sudo -u mexc mkdir -p /opt/mexc/a/data /opt/mexc/a/credentials /opt/mexc/b/data /opt/mexc/b/credentials
sudo -u mexc cp /home/mexc/upload/cycles-export.sqlite3 /opt/mexc/a/data/cycles.sqlite3
sudo -u mexc cp /opt/mexc/b/.env.example /opt/mexc/b/.env
sudo -u mexc chmod 600 /opt/mexc/a/.env /opt/mexc/b/.env /opt/mexc/a/data/cycles.sqlite3
sudo -u mexc chmod 700 /opt/mexc/a/data /opt/mexc/b/data /opt/mexc/a/credentials /opt/mexc/b/credentials
```

Перенесённые ключи Google поместите в `credentials/` соответствующего экземпляра
и установите `chmod 600` на JSON. Проверьте, что файл читается пользователем
`mexc`. Если журнал второго экземпляра переносится отдельно, положите его в
`/opt/mexc/b/data/cycles.sqlite3`.

## 3. Настройте каждый экземпляр

Создайте **своё** виртуальное окружение в каждом каталоге:

```bash
sudo -u mexc bash -c 'cd /opt/mexc/a && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
sudo -u mexc bash -c 'cd /opt/mexc/b && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
```

В каждом `.env` проверьте следующие поля. Значения для `a` и `b` независимы:

```bash
sudo -u mexc nano /opt/mexc/a/.env
sudo -u mexc nano /opt/mexc/b/.env
```

```dotenv
CYCLE_DB=data/cycles.sqlite3
SELLER_CHECK_MODE=adspower
ADSPOWER_BASE_URL=http://127.0.0.1:50325
ADSPOWER_API_KEY=ключ_локального_API_AdsPower
ADSPOWER_P1_PROFILE_ID=ID_своего_профиля_П1
TELEGRAM_BOT_TOKEN=токен_своего_бота
TELEGRAM_CHAT_ID=ID_чата
TELEGRAM_CONTROL_USER_ID=ID_владельца
GOOGLE_SERVICE_ACCOUNT_FILE=credentials/google-service-account.json
GOOGLE_SHEET_TAB=отдельная_вкладка_экземпляра
LOG_TO_FILE=false
AUTO_RESUME_ON_BOOT=true
ENABLE_STATE_CHANGES=true
```

Также проверьте ключи и `MEMBER_ID` П1/П2, ники П2, номера объявлений,
`MEXC_P2..._PROXY_URL`, адреса депозита П1 и `ROLLOVER_PROFILES`. В одном
экземпляре может быть несколько П2. Для независимых процессов нужны разные
аккаунты П1, объявления и боты Telegram; одинаковый Google-файл допустим при
разных вкладках. Пути Windows вида `C:\...` замените на Linux-пути. Оба
экземпляра могут обращаться к одному Local API AdsPower, если ID профилей П1
разные и оба профиля доступны.

## 4. Автозапуск AdsPower и двух ботов

Сверьте путь к `adspower_global` и скопируйте шаблоны:

```bash
sudo install -d -m 700 /etc/mexc
sudo cp /opt/mexc/a/deploy/adspower-headless.service.example /etc/systemd/system/adspower-headless.service
sudo cp /opt/mexc/a/deploy/mexc@.service.example /etc/systemd/system/mexc@.service
sudoedit /etc/mexc/adspower.env
```

Содержимое `/etc/mexc/adspower.env`:

```dotenv
ADSPOWER_API_KEY=тот_же_ключ_что_в_обоих_.env
ADSPOWER_API_PORT=50325
```

```bash
sudo chmod 600 /etc/mexc/adspower.env
sudo systemctl daemon-reload
sudo systemctl enable --now adspower-headless.service
sudo systemctl status adspower-headless.service
sudo systemctl enable --now mexc@a.service mexc@b.service
sudo systemctl status mexc@a.service mexc@b.service
```

Перед запуском каждого бота `ExecStartPre` через Local API открывает нужный
профиль П1 и убеждается, что он активен. Это **не выполняет вход в MEXC**:
сохранённую авторизацию и страницу ордеров нужно проверить самому после
миграции. Если профиль не стартует, служба продолжит попытки, а операции не
начнутся. После успешного запуска отправьте `/start` каждому боту и проверьте
отдельные статусы. После ручной остановки на Windows нажмите «Продолжить» один
раз на сервере. При следующих рестартах работавшая серия возобновится сама;
явная остановка кнопкой Telegram сохранит паузу.

Шаблон AdsPower запускает браузер под `xvfb-run`: режим `--headless=true`
убирает интерфейс приложения, но SunBrowser на сервере без рабочего стола
может требовать X-дисплей для графического процесса. Проверьте, что `xvfb`
и `xauth` установлены, а профиль открывается командой
`.venv/bin/python deploy/start_adspower_profile.py` до запуска бота.

## 5. Логи не старше двух суток

Службы направляют вывод в отдельное пространство журнала `mexc`; файловый лог
Python отключён через `LOG_TO_FILE=false`. Установите настройки **только этого**
пространства журнала:

```bash
sudo cp /opt/mexc/a/deploy/journald@mexc.conf.example /etc/systemd/journald@mexc.conf
sudo cp /opt/mexc/a/deploy/mexc-log-vacuum.service.example /etc/systemd/system/mexc-log-vacuum.service
sudo cp /opt/mexc/a/deploy/mexc-log-vacuum.timer.example /etc/systemd/system/mexc-log-vacuum.timer
sudo systemctl daemon-reload
sudo systemctl restart systemd-journald@mexc.service
sudo systemctl enable --now mexc-log-vacuum.timer
sudo systemctl start mexc-log-vacuum.service
sudo journalctl --namespace=mexc -u mexc@a.service -n 50 --no-pager
sudo journalctl --namespace=mexc -u mexc@b.service -n 50 --no-pager
```

`MaxRetentionSec=2day` ограничивает срок хранения, а часовой таймер ротирует
журнал и удаляет архивы старше 46 часов: даже с задержкой до следующего запуска
таймера записи не должны оставаться дольше двух суток при работающем systemd.
`SystemMaxUse=200M` дополнительно ограничивает объём. Эти настройки не удаляют
`data/cycles.sqlite3`: это журнал сделок и таймеров, необходимый для безопасного
продолжения, а не технический лог. Внутренние файлы логов самого AdsPower в его
каталоге управляются AdsPower отдельно.

## 6. Проверка после перезагрузки

```bash
sudo reboot
```

После подключения обратно:

```bash
systemctl is-active adspower-headless.service mexc@a.service mexc@b.service
sudo journalctl --namespace=mexc -u mexc@a.service --since '10 minutes ago' --no-pager
sudo journalctl --namespace=mexc -u mexc@b.service --since '10 minutes ago' --no-pager
```

В Telegram проверьте `/start` у **обоих** ботов и названия П2. Не проверяйте
работоспособность созданием сделки, пока не сверите незавершённые ордера и
AdsPower-профили. Для безопасной проверки соединения AdsPower можно использовать
`adspower-check ID_УЖЕ_ОТКРЫТОГО_ОРДЕРА`: команда только читает страницу.
