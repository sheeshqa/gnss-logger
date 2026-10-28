# Stage 2.3 — аппаратная приёмка Windows 10

Проверяемая версия: **0.4.0.dev3**. Все аппаратные пункты ниже — **NOT TESTED** до
исполнения пользователем. Разработка не имеет доступа к рабочему ноутбуку/COM3.
Команды выполнять вручную. Они не включают настройки RAWX/rate/config, reset или reconnect.

## 1. Передать код и обновить рабочий ноутбук

Commit/push агент не выполнял. Сначала проверить изменения на домашнем ноутбуке:

```powershell
git status --short
git diff --stat
git diff --check
```

После отдельного решения о публикации выбирать исходники/тесты/документацию явно;
не добавлять data, reports, hardware artifacts, личные конфиги и бинарные наблюдения.
Не использовать git add -f. Для рабочего клона после публикации:

```powershell
$Base = Join-Path $env:LOCALAPPDATA 'GNSS'
$Code = Join-Path $Base 'src\gnss-logger'
Set-Location -LiteralPath $Code
if (git status --porcelain) { throw 'Есть локальные изменения: сначала разобраться, без reset/clean' }
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'Обновление не завершено' }
$Py = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
& $Py -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Ошибка установки' }
& $Py -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Ошибка зависимостей' }
& $Py -m gnss_logger --version
& $Py -m gnss_logger record --help
```

Ожидается 0.4.0.dev3 и --daily-root. Сохранить свою venv Python 3.15.1 x64; домашняя
Python 3.12 не переносится. Если ваш clone имеет иной путь, изменить только $Code.
Перед обновлением остановить работающие Logger и дождаться финализации.

## 2. Выделить новые каталоги и сохранить окружение

```powershell
$Runs = Join-Path $Base 'internal\runs'
$Daily = Join-Path $Base 'data'
$Audit = Join-Path $Base ('hardware-stage2_3\' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Audit | Out-Null
git rev-parse HEAD | Out-File (Join-Path $Audit 'commit.txt') -Encoding utf8
& $Py -c "import sys,platform,struct; print(sys.version); print(sys.executable); print(platform.platform()); print(struct.calcsize('P')*8)" |
    Out-File (Join-Path $Audit 'environment.txt') -Encoding utf8
& $Py -m pip freeze | Out-File (Join-Path $Audit 'packages.txt') -Encoding utf8
& $Py -B -m unittest discover -s tests -v 2>&1 |
    Out-File (Join-Path $Audit 'autonomous-tests.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Автономные тесты не прошли: оборудование пока не тестировать' }
Get-PSDrive -PSProvider FileSystem
```

Ожидается 177 тестов: без личного большого UBX допустим только skip
test_supplied_log_regression (176 успешных + 1 skip). Это первый фактический runtime-тест
нового кода под вашим Python 3.15.1; домашний прогон его не заменяет.
Корни Runs/Daily не должны совпадать или быть вложены друг в друга. Старые данные не
перемещать и не очищать. Заранее обеспечить место для primary + daily (примерно удвоенный
payload плюс индексы), питание и вручную выключить сон на время испытания.

## 3. Идентификация порта и короткий Stage 1 baseline

Закрыть u-center/терминалы, подключить штатный POWER+GPS и антенну.

```powershell
& $Py -m gnss_logger ports --format json --json (Join-Path $Audit 'ports.json') --session-dir (Join-Path $Audit 'stage1-sessions')
if ($LASTEXITCODE -ne 0) { throw 'Порты не перечислены' }
$Ports = (Get-Content (Join-Path $Audit 'ports.json') -Raw -Encoding utf8 | ConvertFrom-Json).ports
$Ports | Format-Table device,description,vid,pid,serial_number,location
$Port = Read-Host 'COM фактически подключённого ZED-F9P; прежний COM3 не гарантируется'
$Chosen = @($Ports | Where-Object { $_.device -eq $Port })
if ($Chosen.Count -ne 1) { throw 'Нет однозначного порта' }
$Usb = $Chosen[0]
if ($null -eq $Usb.vid -or $null -eq $Usb.pid) { throw 'Нет USB VID/PID' }
$Binding = @('--model','ZED-F9P','--vid',('0x{0:X4}' -f [int]$Usb.vid),'--pid',('0x{0:X4}' -f [int]$Usb.pid))
if ($Usb.serial_number) { $Binding += @('--usb-serial',[string]$Usb.serial_number) }
elseif ($Usb.location) { $Binding += @('--usb-location',[string]$Usb.location) }
else { throw 'Нет serial/location для binding' }
& $Py -m gnss_logger serial --port $Port --baud 115200 --seconds 15 --poll-mon-ver `
    --json (Join-Path $Audit 'baseline.json') --session-dir (Join-Path $Audit 'stage1-sessions')
if ($LASTEXITCODE -ne 0) { throw 'Проверить baseline до длительной записи' }
$Common = @('--output',$Runs,'--daily-root',$Daily,'--port',$Port,'--baud','115200','--expected-hz','1','--station','UBX1','--queue-bytes','8388608','--sync-interval','1') + $Binding
```

Сверить MON-VER: ожидаемые по прежним испытаниям ZED-F9P/HPG 1.13/27.12, но фиксировать
фактические значения. Видимый поток RAWX/SFRBX обязателен; программа не включает сообщения.
Binding по location слабее серийного номера: модель/VID/PID/топология не различат два
одинаковых экземпляра, поочерёдно подключённых в тот же разъём. Подтверждать устройство вручную.

## 4. Функция запуска с сохранением stdout/stderr

Определить один раз в текущем PowerShell. Имена отчётов внутри Audit не перезаписываются.

```powershell
function Start-GnssTest {
    param([string]$Name, [int]$Seconds = 1800, [switch]$UntilStopped)
    $ReportPath = Join-Path $Audit ($Name + '.json')
    $ErrorPath = Join-Path $Audit ($Name + '.stderr.txt')
    if ((Test-Path -LiteralPath $ReportPath) -or (Test-Path -LiteralPath $ErrorPath)) { throw 'Имя теста уже занято' }
    $script:BeforeRuns = @(Get-ChildItem -LiteralPath $Runs -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
    $DurationArgs = @('--seconds', [string]$Seconds)
    if ($UntilStopped) { $DurationArgs = @('--until-stopped') }
    & $Py -m gnss_logger record @Common @DurationArgs 2> $ErrorPath | Out-File -LiteralPath $ReportPath -Encoding utf8
    $CodeResult = $LASTEXITCODE
    ($Name + '=' + $CodeResult) | Out-File (Join-Path $Audit 'exit-codes.txt') -Append -Encoding utf8
    $script:LastReport = Get-Content -LiteralPath $ReportPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($null -eq $LastReport -or $LastReport.schema_version -ne 2) { throw 'Не получен diagnostic v2; сохранить stderr и остановить проверку' }
    $script:LastAttempt = $LastReport.extensions.'gnss_logger.recording'.paths.attempt
    $LastReport | Select-Object state,registration,phase_quality,schema_version
    $LastReport.accounting | Format-List
    $LastReport.projection | Format-List
    Write-Host ('exit=' + $CodeResult + '; attempt=' + $LastAttempt)
}
```

## 5. Основная запись — 30 или 60 минут

```powershell
Start-GnssTest -Name 'record30' -Seconds 1800
# Отдельный необязательный более длинный запуск:
# Start-GnssTest -Name 'record60' -Seconds 3600
& $Py -B tools\check_stage2_3_record.py verify $LastAttempt |
    Out-File (Join-Path $Audit 'record30-file-verify.json') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Проверка файлов не прошла' }
$Manifest = Get-Content (Join-Path $LastAttempt 'manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
$Primary = @($Manifest.artifacts | Where-Object { $_.kind -eq 'PRIMARY' })[0].path
Get-FileHash -LiteralPath $Primary -Algorithm SHA256 |
    Format-List | Out-File (Join-Path $Audit 'record30-primary-sha.txt') -Encoding utf8
& $Py -m gnss_logger replay $Primary --speed 0 --json (Join-Path $Audit 'record30-replay.json') --session-dir (Join-Path $Audit 'stage1-sessions')
```

Если выбран record60 вместо record30, заменить имена новых файлов отчёта соответственно.
Offline verify — инструмент в tools; основная команда `gnss-logger verify` ещё недоступна.
Инструмент проверяет terminal manifest, хеши primary/derived/indexes, владельцев, offsets,
побайтовые диапазоны и целые сообщения; его PASS означает файловую проверку, не приёмку
оборудования/фазы. Manifest — конечный commit, один stdout JSON недостаточен.

## 6. Настоящая UTC-полночь

Запустить отдельный 30-минутный тест примерно в 23:50 UTC. Время смотреть так:

```powershell
[DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss')
# Выполнить вручную в окне, пересекающем UTC-полночь:
Start-GnssTest -Name 'midnight' -Seconds 1800
& $Py -B tools\check_stage2_3_record.py verify $LastAttempt |
    Out-File (Join-Path $Audit 'midnight-file-verify.json') -Encoding utf8
```

Не переводить часы приёмника/Windows. Ожидаются минимум два датированных artifact records
с соседними UTC-датами, события TIME_BINDING/ROTATION и корректные первые/последние RAWX.
Небольшой начальный undated содержит сообщения до подтверждения времени. Новый день
ожидает подтверждающий RAWX; если остановиться до него, спорный конец попадёт в undated.
Это явная консервативная политика, а не потеря сообщений.

## 7. Ctrl+C, USB и ручной повторный запуск

```powershell
Start-GnssTest -Name 'ctrlc' -UntilStopped
# Во время этой команды нажать Ctrl+C через 2–3 минуты ОДИН раз и дождаться возврата.
$NewRuns = @(Get-ChildItem -LiteralPath $Runs -Directory | Where-Object { $_.Name -notin $BeforeRuns })
if ($NewRuns.Count -ne 1) { throw 'Новый run не определён однозначно; сохранить результаты и уточнить' }
$NewAttempts = @(Get-ChildItem -LiteralPath (Join-Path $NewRuns[0].FullName 'attempts') -Directory)
if ($NewAttempts.Count -ne 1) { throw 'Неоднозначный attempt' }
$LastAttempt = $NewAttempts[0].FullName
$LastReport = Get-Content (Join-Path $LastAttempt 'report.json') -Raw -Encoding utf8 | ConvertFrom-Json
& $Py -B tools\check_stage2_3_record.py verify $LastAttempt |
    Out-File (Join-Path $Audit 'ctrlc-file-verify.json') -Encoding utf8

Start-GnssTest -Name 'usb' -UntilStopped
# Во время этой команды отключить только USB, записать UTC события и дождаться остановки.
& $Py -B tools\check_stage2_3_record.py verify $LastAttempt |
    Out-File (Join-Path $Audit 'usb-file-verify.json') -Encoding utf8
```

Каждый блок выполнять отдельно: комментарий описывает действие, пока предыдущая команда
работает. Ctrl+C ожидаемо даёт 130/PARTIAL; USB — 1/source_error/PARTIAL. Синхронизированные
файлы могут остаться .ubx.part с CLOSED_PARTIAL; не переименовывать их вручную. Verify
может дать файловый PASS при частичном run, но не должен скрывать его registration_exit_code.
Если verify дал FAIL, сохранить весь bundle, не исправлять его вручную.

Если Ctrl+C прервал и саму функцию PowerShell, отчёт прочитать явными командами выше;
они выбирают ровно один новый run относительно снимка BeforeRuns, не «последний похожий».
Код программы есть в extensions.gnss_logger.recording.exit_code. Не использовать
оставшийся от предыдущего запуска LastAttempt.

Подключить USB снова, создать новый Audit из раздела 2, повторить перечисление/выбор
порта и baseline из раздела 3, заново собрать Binding/Common. Затем:

```powershell
Start-GnssTest -Name 'manual-restart' -Seconds 120
& $Py -B tools\check_stage2_3_record.py verify $LastAttempt |
    Out-File (Join-Path $Audit 'manual-restart-file-verify.json') -Encoding utf8
```

Ожидается новый run/attempt и следующий суффикс для занятой даты, без append/перезаписи.
Перед повторным запуском полезно сохранить Get-FileHash существующих UBX/.part и сравнить
после. Автоматического reconnect нет; промежуток между runs не является непрерывной записью.
Не вызывать recovery над старым run. Аппаратный force kill пока необязателен: синтетический
тест уже есть, а физический сценарий требует отдельного окна и не доказывает power-loss safety.

## 8. Приёмка и передаваемые результаты

| Проверка | Критерий | До запуска |
|---|---|---|
| Runtime | 0.4.0.dev3, сохранены commit/версии, автономные тесты проходят на рабочем Python | NOT TESTED |
| Основная запись | exit 0, STOPPED_COMPLETE, received=written=synced, pending/unarchived=0, primary integrity VERIFIED | NOT TESTED |
| Проекция | finished=true, failed=false, backlog/pending=0, verified_artifacts=closed_artifacts; offline verify PASS | NOT TESTED |
| RAWX/SFRBX | наблюдения около 1 Гц, без внутренних пропусков/checksum/decode errors; Replay counters совпали | NOT TESTED |
| Полночь | соседние UTC-даты, правильные RAWX dates, whole frames, source coverage без потерь/дублей | NOT TESTED |
| Ctrl+C/USB | ожидаемые partial/code/reason, все received учтены, terminal manifest, проверенные bytes/SHA | NOT TESTED |
| Ручной новый run | новый suffix/IDs, прежние файлы и хеши неизменны, соединение подтверждено заново | NOT TESTED |

Фазовые WARNINGS сами по себе не ошибка регистрации и не доказанные cycle slips. Число
полных эпох сопоставлять с их крайними receiver UTC, а не требовать ровно N при произвольном
моменте старта/остановки. Один сеанс на 30 минут не проверяет работу 3–7 суток или X20P.

Передать отдельно от Git: Audit, полные attempts (manifest.start/final, report, events,
checkpoints, primary, receipts/ranges, artifacts/*.json) и все связанные dated/undated UBX/.part.
Для копий сохранить SHA до/после передачи. Пути в metadata относятся к рабочему ноутбуку;
при переносе не править originals, а сообщить соответствие старого корня новому месту.
Только после анализа фактических файлов можно принимать решение об аппаратном PASS Stage 2.3.
