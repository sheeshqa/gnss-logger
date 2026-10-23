# GNSS Logger: перенос на Windows 10 x64

Дата подготовки: 2026-10-23. Версия: **0.4.0.dev1**, Stage 2.1.
Длительная регистрация ещё не реализована. record/status/verify/recover возвращают
4 / NOT_IMPLEMENTED. Аппаратный smoke-test ниже использует существующий serial/replay.
Все команды установки/аппаратного теста на рабочем ноутбуке выполняет пользователь.

## 1. Что подготовлено и что не опубликовано

В домашнем каталоге проекта Git-репозитория нет: git rev-parse/status/remote возвращают
not a git repository. Git установлен. Новый .git в рабочем проекте не создавался, remote
не назначался, commit/push и обращение к приватному репозиторию не выполнялись.
Временный локальный Git использован только в отдельной проверочной копии для проверки ignore.

.gitignore исключает .venv/пакетные сборки, исходные UBX/RINEX/CRINEX и сжатые варианты,
данные/логи/part/JSONL, аппаратные артефакты, docs/evidence, локальные настройки и секреты.
Личные hardware reports/user-context и существующий station.example.json с координатами
остаются дома; публично переносимый config/station.template.json содержит null вместо координат.
Исключение из запрета UBX — ровно три MOCK fixture и их sidecar в tests/fixtures:
они синтетические, нужны автономным тестам, не являются измерениями приёмника.

Ignore не очищает уже созданную историю и не запрещает git add -f. Перед первым push
проверяется полный staged список. Пароли/токены в файлах, командах и URL не сохраняются.

## 2. Ручные действия на GitHub и домашнем компьютере

**Выполнить только после отдельного подтверждения публикации.**

1. На GitHub создать **Private** репозиторий gnss-logger. При переносе существующего кода
   оставить его пустым: без автоматически созданных README, .gitignore и license.
2. Выбрать аккаунт/организацию и проверить доступ рабочего аккаунта к этому Private repo.
3. На домашнем компьютере ещё раз проверить git rev-parse --show-toplevel в каталоге проекта.
   Если найден существующий Git, использовать его и сначала проверить status/remote/history;
   не выполнять повторную инициализацию или замену remote автоматически.
4. Если Git по-прежнему отсутствует, локально инициализировать репозиторий, проверить состав,
   сделать commit и только затем назначить подтверждённый URL и выполнить push.

Пример будущих команд из домашнего каталога проекта (адрес OWNER заменить):

```powershell
git init -b main
git add -- .gitignore pyproject.toml README.md gnss_logger tests tools config docs stage1_report.md
git status --short
git diff --cached --stat
git diff --cached --name-only
git diff --cached --check
git diff --cached
```

В списке не должно быть .venv, data/reports, hardware-test artifacts, docs/evidence,
station.example.json, .env, токенов, настоящих UBX/RINEX. Допустимы только три названных
MOCK_*.ubx и sidecar; нельзя разрешать все UBX внутри tests общим wildcard.
Просмотреть diff на секреты и персональные сведения. Автоматический поиск не гарантирует
обнаружение всех секретов. При неожиданных файлах остановиться до commit/push.

После проверки, с собственными Git name/email (учитывать приватность email):

```powershell
git config user.name 'YOUR NAME'
git config user.email 'YOUR VERIFIED OR GITHUB NOREPLY EMAIL'
git commit -m 'Prepare GNSS Logger 0.4.0.dev1 for Windows deployment'
git remote add origin https://github.com/OWNER/gnss-logger.git
git remote -v
git push -u origin main
```

Пример относится только к новому пустому repo/main. Если репозиторий уже создан/не пустой,
сначала согласовать его реальное состояние; force push не использовать.

## 3. Python и Git на рабочем ноутбуке

Установить поддерживаемый в организации **Python 3.12 x64**, стандартный runtime с pip/venv,
не embeddable package. Источник: [официальная инструкция Python](https://docs.python.org/3/using/windows.html).
Python Install Manager поддерживает Windows 10; при его использовании:

```powershell
pymanager install 3.12
py -3.12 -c "import sys,struct; print(sys.version); print(sys.executable); assert sys.version_info[:2] == (3,12); assert struct.calcsize('P') == 8"
```

Если установлен классический Python/launcher, install-команда не нужна; выполнить проверку
py -3.12. При конфликте launcher использовать полный путь к установленному python.exe.
Не удалять старый Python 3.9 ради GNSS Logger: виртуальное окружение создаётся конкретным 3.12.
Не требовать именно домашний 3.12.14: он из bundled runtime. Фактическую patch-версию записать.
У классического CPython 3.12.10 последний полный Windows installer; последующие релизы
3.12 security-only могут не содержать обычного установщика.
[Страница Python 3.12.10](https://www.python.org/downloads/release/python-31210/).

Установить [Git for Windows x64](https://git-scm.com/install/windows), оставить доступ Git
из PowerShell и Git Credential Manager. Открыть новое окно PowerShell:

```powershell
git --version
py -3.12 --version
```

При HTTPS-клонировании Private repo войти через браузер/Git Credential Manager и пройти 2FA.
Учетные данные хранятся вне repo в Windows Credential Manager. Не использовать URL с PAT,
credential.helper=store или пароль GitHub как Git-пароль.
[Документация GitHub](https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git).

## 4. Разделение кода и данных

Предлагаемая структура, доступная обычному пользователю и вне OneDrive:

```text
%LOCALAPPDATA%/GNSS/
  src/gnss-logger/       # только Git-код и собственная .venv
  data/                 # UBX и результаты; НЕ внутри Git-клона
  config/               # локальные настройки станции
  deployment/           # версии, вывод установки и тестов
```

При наличии выделенного NTFS-диска DataRoot можно заменить, например, на D:\GNSSData.
Проверить свободное место. Программа не удаляет старые наблюдения ради освобождения диска.
Домашнюю .venv и весь домашний каталог data/reports на ноутбук не копировать.

После публикации и предоставления доступа:

```powershell
$Base = Join-Path $env:LOCALAPPDATA 'GNSS'
$Code = Join-Path $Base 'src\gnss-logger'
$DataRoot = Join-Path $Base 'data'
$LocalConfig = Join-Path $Base 'config'
$Deploy = Join-Path $Base 'deployment'
New-Item -ItemType Directory -Force -Path (Split-Path $Code),$DataRoot,$LocalConfig,$Deploy | Out-Null
if (Test-Path -LiteralPath $Code) { throw 'Каталог кода уже существует: проверьте его, не клонируйте поверх.' }
git clone https://github.com/OWNER/gnss-logger.git $Code
if ($LASTEXITCODE -ne 0) { throw 'Клонирование не выполнено' }
Set-Location -LiteralPath $Code
git status --short
git rev-parse HEAD
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'venv не создан' }
$Py = Join-Path $Code '.venv\Scripts\python.exe'
& $Py -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Установка не выполнена' }
& $Py -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Нарушены зависимости' }
& $Py -m gnss_logger --version
```

Activation.ps1 не нужен; менять ExecutionPolicy не требуется. pip использует изолированную
сборку с setuptools>=68; интернет нужен для build dependencies и pyserial==3.5.
Не использовать --no-build-isolation в чистой venv без установленного build backend.
pyproject.toml не содержит домашних путей; runtime-зависимость одна: pyserial==3.5.
Editable-установка предназначена для исходного checkout: после обновления/смены пути
повторно выполнить pip install -e ., не переносить готовую .venv между компьютерами.

При необходимости создать личную конфигурацию из нейтрального шаблона:

```powershell
$Station = Join-Path $LocalConfig 'station.local.json'
if (-not (Test-Path -LiteralPath $Station)) {
    Copy-Item -LiteralPath '.\config\station.template.json' -Destination $Station
}
```

Это карточка для будущего использования: текущий CLI её автоматически не загружает.
Не копировать реальные координаты обратно в публикуемый template.

## 5. Автономная проверка установки

Из корня клона:

```powershell
$Stamp = [guid]::NewGuid().ToString('N')
$CheckDir = Join-Path $Deploy $Stamp
New-Item -ItemType Directory -Path $CheckDir | Out-Null
git rev-parse HEAD | Out-File (Join-Path $CheckDir 'commit.txt') -Encoding utf8
& $Py -c "import sys,platform,struct; print(sys.version); print(platform.platform()); print(struct.calcsize('P')*8)" |
    Out-File (Join-Path $CheckDir 'environment.txt') -Encoding utf8
& $Py -m pip freeze | Out-File (Join-Path $CheckDir 'packages.txt') -Encoding utf8
& $Py -B -m unittest discover -s tests -v 2>&1 |
    Out-File (Join-Path $CheckDir 'tests.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Тесты не прошли; аппаратный запуск отложить' }
& $Py -m gnss_logger mock --profile f9p --session-dir (Join-Path $CheckDir 'mock-sessions') --json (Join-Path $CheckDir 'mock.json')
if ($LASTEXITCODE -ne 0) { throw 'Mock smoke не прошёл' }
```

Ожидается 111 тестов, **один skip** необязательного домашнего реального лога, остальные 110 OK.
Все синтетические fixtures должны присутствовать. Пропуск из-за отсутствующего RAWX fixture
не предусмотрен. Реальные hardware-tests и домашние absolute paths для тестов не нужны.
Никакой аппаратный PASS из этого результата не следует.

Программа принимает пути capture/json/session-dir от CLI; defaults reports/sessions зависят
от текущего каталога, а не от компьютера разработчика. Поэтому для эксплуатации всегда
передавать абсолютные пути вне клона. Автономные тесты ожидают запуск из корня checkout.

## 6. Ручной smoke-test реального ZED-F9P

Закрыть u-center/терминалы и другие владельцы COM. Подключить антенну, затем POWER+GPS.
Команды ниже разрешают только пассивное чтение и MON-VER/RAM VALGET. Конфигурация не меняется.
Номер COM выбрать заново; COM10 не предполагается постоянным.

```powershell
$Run = Join-Path $DataRoot ('smoke_' + [guid]::NewGuid().ToString('N'))
$Audit = Join-Path $Run 'sessions'
New-Item -ItemType Directory -Path $Run | Out-Null
& $Py -m gnss_logger ports --format json --json (Join-Path $Run 'ports.json') --session-dir $Audit
if ($LASTEXITCODE -ne 0) { throw 'Ошибка инвентаризации портов' }
Get-Content (Join-Path $Run 'ports.json') -Raw -Encoding utf8
$Port = Read-Host 'Укажите проверенный COM-порт приёмника'

& $Py -m gnss_logger serial --port $Port --baud 115200 --seconds 30 --inspect --request-timeout 5 --capture (Join-Path $Run 'inspect.ubx') --json (Join-Path $Run 'inspect.json') --session-dir $Audit
$InspectExit = $LASTEXITCODE
"inspect=$InspectExit" | Add-Content (Join-Path $Run 'exit_codes.txt') -Encoding utf8
if ($InspectExit -ne 0) { throw 'Inspect требует анализа; не считать smoke успешным' }

& $Py -m gnss_logger serial --port $Port --baud 115200 --seconds 120 --expected-hz 1 --capture (Join-Path $Run 'passive.ubx') --json (Join-Path $Run 'passive.json') --session-dir $Audit
$SerialExit = $LASTEXITCODE
"serial=$SerialExit" | Add-Content (Join-Path $Run 'exit_codes.txt') -Encoding utf8
if ($SerialExit -ne 0) { throw 'Пассивная регистрация требует анализа' }

& $Py -m gnss_logger replay (Join-Path $Run 'passive.ubx') --speed 0 --json (Join-Path $Run 'replay.json') --session-dir $Audit
$ReplayExit = $LASTEXITCODE
"replay=$ReplayExit" | Add-Content (Join-Path $Run 'exit_codes.txt') -Encoding utf8
if ($ReplayExit -ne 0) { throw 'Replay требует анализа' }

$Live = Get-Content (Join-Path $Run 'passive.json') -Raw -Encoding utf8 | ConvertFrom-Json
$Replay = Get-Content (Join-Path $Run 'replay.json') -Raw -Encoding utf8 | ConvertFrom-Json
$Hash = (Get-FileHash (Join-Path $Run 'passive.ubx') -Algorithm SHA256).Hash
$Hash | Out-File (Join-Path $Run 'passive_sha256.txt') -Encoding utf8
if ($Hash -ine $Live.run.stream_sha256 -or $Hash -ine $Live.artifacts[0].sha256 -or $Hash -ine $Replay.run.stream_sha256) {
    throw 'Расхождение SHA-256'
}
$Live.artifacts | Format-List
$Live.RAWX | Format-List
$Live.SFRBX | Format-List
$Live.timing | Format-List
$Live.phase_quality | Format-List
```

Критерии ручной приёмки: MON-VER подтверждает F9P/FW; оба read response получены;
RAM RAWX-USB/SFRBX-USB=1 и RATE-MEAS=1000 при исходной конфигурации 1 Гц; поток
RAWX/SFRBX присутствует; примерно 120 эпох, интервалы около 1 с, пропуски/ошибки разобраны;
closed_complete, matches_received_stream=true, unarchived_bytes=0; совпадают hash/bytes,
messages/RAWX/SFRBX/timing Live и Replay; manifest и журнал доступны.
Код 0 сам по себе не является приёмкой. Phase WARNINGS отдельно от целостности записи.
Если сообщения отключены, CLI их не включит. Сохранить весь $Run для анализа.
Аппаратный статус новой среды остаётся NOT TESTED до проверки этих результатов.

## 7. Обновления через Git без затрагивания данных

1. Завершить текущую регистрацию Ctrl+C, дождаться финального JSON и закрытия файлов.
   Не обновлять исходники/venv в работающем процессе. Проверить, что DataRoot находится
   вне $Code; эту структуру сохранять при всех обновлениях.
2. В корне клона проверить git status. При локальных правках остановиться и разобраться;
   не применять reset --hard, git clean -fdx или force checkout.
3. Обновиться только fast-forward, переустановить editable и повторить тесты.

```powershell
Set-Location -LiteralPath $Code
if (git status --porcelain) { throw 'Есть локальные изменения; обновление остановлено' }
$Before = git rev-parse HEAD
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'Обновление не выполнено; проверить ветку и права' }
git rev-parse HEAD
& $Py -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Установка обновления не прошла' }
& $Py -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Ошибка зависимостей' }
& $Py -m gnss_logger --version
& $Py -B -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Регрессия после обновления; запись не запускать' }
```

Git обновляет $Code, не соседний DataRoot. Проверить сохранность последней закрытой записи
сравнением Get-FileHash с её manifest. Для отката проверять старый commit в **отдельном**
каталоге/venv (git worktree add --detach <new-code-path> <commit>), сохранив текущий clone
и данные; не применять разрушительный reset. Аппаратный smoke повторять после изменений
транспорта/записи и существенных изменений среды. Старые отчёты не перезаписывать.

## 8. Что передать после установки

Commit ID, --version, environment/packages/tests из $CheckDir, коды завершения, весь smoke
$Run. Отдельно сообщить Windows build, FW, способ подключения и свободное место.
Исходные данные передаются отдельно при необходимости, не через GitHub. Ничего не удалять.
Многосуточный аппаратный прогон сейчас не начинать: работающий Stage 2.2 ещё не реализован.
