# Stage 2.2 — обновление и аппаратная приёмка Windows 10

Дата: 2026-10-27. Проверяемая версия: **0.4.0.dev2**. Статус до исполнения: **NOT TESTED**.
Команды аппаратного теста выполняет пользователь. Агент их не запускал.
Требуется отдельная приёмка нового record; прежний успешный serial 0.4.0.dev1 её не заменяет.

## 1. Опубликовать с домашнего компьютера — только после подтверждения

Из существующего домашнего Git-клона (новый git init не нужен):

```powershell
git status --short
git diff --stat
git diff --check
git diff -- gnss_logger pyproject.toml tests README.md docs tools
git add -- .gitignore gnss_logger/__init__.py gnss_logger/recording/cli.py gnss_logger/recording/controller.py gnss_logger/recording/reader.py gnss_logger/recording/storage.py pyproject.toml tests/test_stage2_contracts.py tests/test_stage2_record.py tools/check_stage2_2_record.py README.md docs/stage2_2_report.md docs/stage2_2_hardware_test.md docs/stage2_1_contracts.md docs/windows10_deployment.md
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
git diff --cached
```

Проверить, что staged не содержит реальных UBX/RINEX, .venv, config/*.local.*, hardware
artifacts, docs/evidence или reports. Синтетические исходники тестов допустимы.
Не использовать git add -f и не включать private конфиги. Реальные данные остаются на диске.
После проверки и подтверждения публикации:

```powershell
git commit -m 'Implement Stage 2.2 primary GNSS recording'
git push origin main
git rev-parse HEAD
```

Агент commit/push не выполнял. Если текущая ветка отличается от main или есть посторонние
правки, сначала проверить их; не выполнять force push/reset/clean.

## 2. Обновить рабочий ноутбук, сохранив Python 3.15

Остановить все работающие экземпляры Logger и дождаться закрытия файлов. Код и данные
должны оставаться в соседних каталогах вне OneDrive. Если использована прежняя схема:

```powershell
$Base = Join-Path $env:LOCALAPPDATA 'GNSS'
$Code = Join-Path $Base 'src\gnss-logger'
$DataRoot = Join-Path $Base 'data'
Set-Location -LiteralPath $Code
if (git status --porcelain) { throw 'Есть локальные правки. Не выполнять reset/clean.' }
$OldCommit = git rev-parse HEAD
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'git pull не завершён' }
$Commit = git rev-parse HEAD
$Py = (Resolve-Path '.\.venv\Scripts\python.exe').Path
& $Py -c "import sys,struct; print(sys.version); print(sys.executable); assert sys.version_info[:2] == (3,15); assert struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw 'Уточнить фактическую версию/разрядность Python' }
& $Py -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Установка обновления не завершена' }
& $Py -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Нарушены зависимости' }
& $Py -m gnss_logger --version
& $Py -m gnss_logger record --help
```

Ожидается 0.4.0.dev2. Существующая venv Python 3.15.1 не заменяется домашней 3.12.
Runtime dependency остаётся pyserial==3.5, build backend устанавливается pip изолированно.
Если пути клона/данных отличаются, изменить только $Code/$DataRoot; данные не переносить ради Git.

## 3. Автономные проверки на Python 3.15 до оборудования

```powershell
$Run = Join-Path $DataRoot ('stage2_2_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Run | Out-Null
$Commit | Out-File (Join-Path $Run 'commit.txt') -Encoding utf8
& $Py -c "import sys,platform; print(sys.version); print(platform.platform())" |
    Out-File (Join-Path $Run 'environment.txt') -Encoding utf8
& $Py -m pip freeze | Out-File (Join-Path $Run 'packages.txt') -Encoding utf8
& $Py -B -m unittest discover -s tests -v 2>&1 |
    Out-File (Join-Path $Run 'autonomous-tests.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Автономные тесты не прошли на этом Python; аппаратуру пока не тестировать' }
& $Py -B tools\check_stage2_2_record.py --output (Join-Path $Run 'SIMULATED')
if ($LASTEXITCODE -ne 0) { throw 'Синтетическая record/Replay проверка не прошла' }
```

В Git-клоне без личного большого UBX ожидается 147 tests, 146 успешных + один ожидаемый
skip test_supplied_log_regression; не должно быть failures/errors или иных skip.
Синтетический инструмент НЕ открывает COM; обе модели помечены SIMULATED и не дают
аппаратного PASS. Именно этот прогон впервые проверит новый runtime под Python 3.15.

## 4. Порт и read-only контроль Stage 1

Закрыть u-center/терминалы. Подключить антенну и POWER+GPS. Убедиться в питании ноутбука,
достаточном месте на локальном диске и вручную отключить автоматический сон на время теста.
В Stage 2.2 нет автоматической защиты от сна/контроля свободного места.

```powershell
$Audit = Join-Path $Run 'stage1-sessions'
& $Py -m gnss_logger ports --format json --json (Join-Path $Run 'ports.json') --session-dir $Audit
if ($LASTEXITCODE -ne 0) { throw 'Инвентаризация не выполнена' }
$Ports = (Get-Content (Join-Path $Run 'ports.json') -Raw -Encoding utf8 | ConvertFrom-Json).ports
$Ports | Format-Table device,description,vid,pid,serial_number,location
$Port = Read-Host 'Проверенный порт ZED-F9P (ранее COM3, но номер может измениться)'
$Chosen = @($Ports | Where-Object { $_.device -eq $Port })
if ($Chosen.Count -ne 1) { throw 'Порт не найден однозначно' }
$Usb = $Chosen[0]
if ($null -eq $Usb.vid -or $null -eq $Usb.pid) { throw 'Нужны реальные USB VID/PID' }
$Binding = @('--model','ZED-F9P','--vid',('0x{0:X4}' -f [int]$Usb.vid),'--pid',('0x{0:X4}' -f [int]$Usb.pid))
if (-not [string]::IsNullOrWhiteSpace([string]$Usb.serial_number)) {
    $Binding += @('--usb-serial',[string]$Usb.serial_number)
} elseif (-not [string]::IsNullOrWhiteSpace([string]$Usb.location)) {
    $Binding += @('--usb-location',[string]$Usb.location)
} else { throw 'Нет serial/location: автоматическую привязку не угадывать' }

& $Py -m gnss_logger serial --port $Port --baud 115200 --seconds 60 --inspect --request-timeout 5 --capture (Join-Path $Run 'baseline.ubx') --json (Join-Path $Run 'baseline.json') --session-dir $Audit
if ($LASTEXITCODE -ne 0) { throw 'Stage 1 baseline требует анализа' }
```

MON-VER должен подтвердить реальный F9P; VALGET — фактическую RAWX/SFRBX/частоту 1 Гц.
Команда serial их не включает и не меняет. record также посылает только MON-VER poll.
При отсутствии USB serial привязка location+VID/PID+MON-VER обозначается USB_TOPOLOGY:
она подтверждает разъём/модель, а не уникальность физического экземпляра. Автоматического
reconnect здесь нет; каждый новый тест выбирает порт/привязку заново.

## 5. Основная запись: 30–60 минут

30 минут:

```powershell
$RecordRoot = Join-Path $Run 'record_30min'
& $Py -m gnss_logger record --output $RecordRoot --port $Port --baud 115200 --seconds 1800 --expected-hz 1 --queue-bytes 8388608 --sync-interval 1 --identity-timeout 5 @Binding
$RecordExit = $LASTEXITCODE
"record_30min=$RecordExit" | Add-Content (Join-Path $Run 'exit_codes.txt') -Encoding utf8
```

Либо отдельный тест 60 минут (новый output, не перезаписывает предыдущий):

```powershell
$RecordRoot = Join-Path $Run 'record_60min'
& $Py -m gnss_logger record --output $RecordRoot --port $Port --baud 115200 --seconds 3600 --expected-hz 1 --queue-bytes 8388608 --sync-interval 1 --identity-timeout 5 @Binding
$RecordExit = $LASTEXITCODE
"record_60min=$RecordExit" | Add-Content (Join-Path $Run 'exit_codes.txt') -Encoding utf8
```

Не перенаправлять record в PowerShell-пайплайн при проверке Ctrl+C: терминал может отдельно
оборвать consumer stdout. Полный report/manifest сохраняются самим record в новом run UUID /
attempt UUID. Последний вывод stdout содержит пути, но не заменяет сохранённый manifest.
До остановки primary имеет .ubx.part; после штатного закрытия — .ubx. При ошибке/Ctrl+C
остаётся partial; расширение само по себе не является критерием целостности.

Таймер включает подготовку/идентификацию. Число эпох около 1800/3600, а не гарантированно
ровно столько; анализировать реальный первый/последний RAWX и интервалы отдельно.
Синхронизация 1 с — целевой период вызовов, не жёсткое real-time обещание Windows/диска.

## 6. Проверка основной записи и Replay

Для выбранного $RecordRoot должен существовать ровно один конечный manifest.json.

```powershell
$Found = @(Get-ChildItem -LiteralPath $RecordRoot -Recurse -Filter manifest.json -File)
if ($Found.Count -ne 1) { throw 'Нет единственного конечного manifest: проверить неполную финализацию' }
$ManifestPath = $Found[0].FullName
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$Attempt = Split-Path $ManifestPath
$Report = Get-Content (Join-Path $Attempt 'report.json') -Raw -Encoding utf8 | ConvertFrom-Json
$Details = $Report.extensions.'gnss_logger.recording'
$Diag = $Report.extensions.'gnss_logger.diagnostics'
$Primary = @($Manifest.artifacts | Where-Object { $_.kind -eq 'PRIMARY' })
if ($Primary.Count -ne 1) { throw 'Неожиданное число primary файлов' }
$Ubx = $Primary[0].path
$Hash = (Get-FileHash -LiteralPath $Ubx -Algorithm SHA256).Hash
if ($Hash -ine $Primary[0].sha256 -or $Hash -ine $Details.received_sha256) { throw 'Расхождение SHA-256' }
if ((Get-Item -LiteralPath $Ubx).Length -ne $Manifest.accounting.received) { throw 'Размер не равен received' }
$Manifest.accounting | Format-List
$Manifest.connections | Format-List
$Details | Format-List
$Diag.RAWX | Format-List
$Diag.SFRBX | Format-List
$Diag.timing | Format-List

& $Py -m gnss_logger replay $Ubx --speed 0 --json (Join-Path $Run 'record_replay.json') --session-dir (Join-Path $Run 'replay-sessions')
if ($LASTEXITCODE -ne 0) { throw 'Replay основной записи требует анализа' }
$Replay = Get-Content (Join-Path $Run 'record_replay.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($Hash -ine $Replay.stream_sha256) { throw 'Replay hash отличается' }
foreach ($Field in @('messages','RAWX','SFRBX','timing')) {
    $A = $Diag.$Field | ConvertTo-Json -Depth 30 -Compress
    $B = $Replay.$Field | ConvertTo-Json -Depth 30 -Compress
    if ($A -cne $B) { throw "Live/Replay отличаются: $Field" }
}
```

Повторный Replay сохранять с новым именем/новым $Run; старые файлы не удалять ради перезапуска.
Phase WARNINGS не является ошибкой сохранения, но предупреждения нужно просмотреть отдельно.

## 7. Непрерывный режим и ручная остановка

```powershell
$Continuous = Join-Path $Run 'record_ctrl_c'
& $Py -m gnss_logger record --output $Continuous --port $Port --baud 115200 --until-stopped --queue-bytes 8388608 --sync-interval 1 @Binding
$LASTEXITCODE
```

Через 30–60 с один раз Ctrl+C и дождаться завершения. Ожидается 130,
stop_reason=keyboard_interrupt, registration=PARTIAL, CLOSED_PARTIAL; при исправном диске
received=journal_written=journal_synced, pending=unarchived=0, SHA и manifest/report/events есть.
Partial здесь означает прерванную сессию, а не обязательное повреждение сохранённых байтов.
Выполнить Replay по пути из её manifest с НОВЫМ именем JSON; сравнить SHA/счётчики.

## 8. Ошибка USB — отдельный тест

```powershell
$Disconnect = Join-Path $Run 'record_usb_disconnect'
& $Py -m gnss_logger record --output $Disconnect --port $Port --baud 115200 --seconds 180 --queue-bytes 8388608 --sync-interval 1 @Binding
$LASTEXITCODE
```

Через ~40 с извлечь USB, записать внешнюю UTC-отметку, дождаться завершения. При исключении
драйвера ожидается source_error, exit 1, CLOSED_PARTIAL; очередь дописана, hashes/учёт/ошибка
сохранены. Автоматического reconnect нет. Если драйвер возвращает пустые чтения до таймера,
финальная проверка даёт source_stale/exit 1/partial при RAWX старше max(5 с, 3/expected-hz)
или SFRBX старше 30 с. Ожидание до таймера пока допустимо: немедленное обнаружение и
переподключение относятся к будущему 2.4.
После возвращения USB сделать новый ports/serial baseline перед новым record.
Старый primary не дописывается и не склеивается с новым; его прежний SHA должен сохраниться.

Ошибки записи/flush/fsync/overflow проверены автономно. На рабочем диске не заполнять
весь том для проверки; отдельные дисковые fault trials относятся к 2.5.
Не выполнять taskkill/отключение питания как штатный Ctrl+C: recovery ещё не реализован.

## 9. Критерии аппаратной приёмки

| Проверка | Критерий |
|---|---|
| Среда | Сохранены commit, Windows build, Python 3.15.x x64, версии Logger/pySerial, вывод тестов |
| Идентификация | Выбранный COM удовлетворяет явному USB binding; реальный MON-VER даёт ZED-F9P |
| Таймер 30/60 мин | duration, exit 0, STOPPED_COMPLETE, VERIFIED; финальный manifest существует |
| Байты | received=journal_written=journal_synced=размер файла, pending=unarchived=0, received_count_exact=true |
| Целостность | SHA actual = manifest = received_sha256 = Replay; файл CLOSED_COMPLETE |
| Поток | RAWX/SFRBX есть, фактический 1 Гц; отсутствующие эпохи/checksum/decode ошибки отсутствуют либо отдельно разобраны |
| Очередь | queue_peak_bytes <= queue_capacity_bytes; нет overflow/ошибок событий/синхронизации |
| Протокол | Все JSON и события валидны по schema v1; очередь/ошибки отражены; phase quality отдельно |
| Ctrl+C | 130/keyboard_interrupt, partial; все возвращённые байты учтены и по возможности сохранены |
| USB | Ошибка/тишина зафиксирована; данные и отчёты сохранены; автоматического продолжения нет |
| Сохранность | Предыдущие данные и их хеши не изменились; ничего не удалено/не отправлено в Git |

Если manifest отсутствует/невалиден, это незавершённая финализация, даже если report.json
существует и primary читается. report.json имеет commit_manifest_required=true: до конечного
manifest его результат предварительный. Если устройство/диск зависло внутри системного вызова,
нет гарантии немедленной остановки; не принуждать удалять/обрезать файл, сохранить артефакты.

Передать весь $Run, включая manifests, reports, events и checkpoints, вне GitHub. По этим
файлам составляется отдельный аппаратный отчёт. Не считать X20P, многосуточную эксплуатацию,
UTC-ротацию/reconnect/recovery проверенными этим тестом. Stage 2.3 требует отдельного согласования.
