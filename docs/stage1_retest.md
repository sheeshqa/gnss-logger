# Короткий аппаратный retest Stage 1 — 0.3.0

Статус до выполнения оператором: **NOT TESTED**. Агент команды ниже не выполнял.
Никаких команд настройки приёмника нет. Закройте u-center и другие владельцы COM.
Подключите антенну до питания, используйте POWER+GPS. Не допускайте сна ноутбука.

## 1. Новый каталог и USB-инвентаризация

```powershell
Set-Location 'C:\Users\vkash\OneDrive\Документы\ChatGPT\software_u-blox'
$Py = (Resolve-Path '.\.venv\Scripts\python.exe').Path
& $Py -m gnss_logger --version

$Run = Join-Path $env:LOCALAPPDATA ('GNSSLogger\retest_03\' + [guid]::NewGuid().ToString('N'))
$Audit = Join-Path $Run 'sessions'
New-Item -ItemType Directory -Force -Path $Run | Out-Null

& $Py -m gnss_logger ports --format json --session-dir $Audit --json "$Run\ports.json"
```

Ожидается версия 0.3.0. Выберите COM из ports.json: USB-кандидат не доказывает модель.
Не используйте Bluetooth COM5 из старого теста. Подтвердите текущий номер вручную:
```powershell
$Port = 'COM10'
```

## 2. Чтение модели/конфигурации и основной таймер

```powershell
& $Py -m gnss_logger serial --port $Port --baud 115200 --seconds 60 --inspect --request-timeout 5 --capture "$Run\inspect.ubx" --json "$Run\inspect.json" --session-dir $Audit
$LASTEXITCODE

& $Py -m gnss_logger serial --port $Port --baud 115200 --seconds 600 --expected-hz 1 --capture "$Run\recording.ubx" --json "$Run\recording.json" --session-dir $Audit
$LASTEXITCODE

& $Py -m gnss_logger replay "$Run\recording.ubx" --speed 0 --json "$Run\recording_replay.json" --session-dir $Audit
$LASTEXITCODE

$Live = Get-Content "$Run\recording.json" -Raw -Encoding utf8 | ConvertFrom-Json
$Replay = Get-Content "$Run\recording_replay.json" -Raw -Encoding utf8 | ConvertFrom-Json
$Hash = (Get-FileHash "$Run\recording.ubx" -Algorithm SHA256).Hash

$Hash -ieq $Live.artifacts[0].sha256
$Hash -ieq $Live.run.stream_sha256
$Hash -ieq $Replay.run.stream_sha256
$Live.artifacts[0] | Format-List
$Live.run | Format-List
$Live.registration
$Live.phase_quality.status
$Live.paths
```

Критерии: ответы MON-VER/VALGET; нет конфигурационных изменений; файл закрыт,
complete=true, matches_received_stream=true, unarchived_bytes=0; хеши/bytes/RAWX/SFRBX
Live и Replay совпадают. Таймер может немного пересечь срок, но уже прочитанные байты
не должны отбрасываться. Проверяйте run.deadline_overrun_s, read_calls и pending_bytes.
Около 600 эпох при 1 Гц — ориентир, а не требование ровно 600 при любой буферизации.

phase_quality=WARNINGS не означает повреждение файла; изучите события отдельно.
NO_INDICATORS_DETECTED не доказывает отсутствие всех cycle slips. Поле hardware status
остаётся NOT TESTED до внешней аппаратной приёмки.

## 3. Ctrl+C и отдельное отключение USB

```powershell
& $Py -m gnss_logger serial --port $Port --seconds 120 --capture "$Run\ctrl_c.ubx" --json "$Run\ctrl_c.json" --session-dir $Audit
```
Один раз нажмите Ctrl+C примерно через 30 с. Ожидается 130, stop_reason=keyboard_interrupt,
registration=PARTIAL, artifacts[0].complete=false, хеш/размер/последние времена чтения сохранены.
В audit-каталоге должны быть manifest.json и events.jsonl; рядом с UBX — .meta.json.

Отдельный сеанс:
```powershell
& $Py -m gnss_logger serial --port $Port --seconds 180 --capture "$Run\disconnect.ubx" --json "$Run\disconnect.json" --session-dir $Audit
```
После появления данных извлеките USB, запишите точное UTC-время и дождитесь ошибки/окончания.
При исключении ожидаются exit 1, source_error, partial-файл с хешем и manifest.
Если драйвер возвращает пустые чтения, возможен deadline и код 3 по stale: это нужно отметить,
а не считать аппаратным успехом по одному полю complete. Повторно Ctrl+C не нажимайте,
не убивайте процесс принудительно во время финализации.

После завершения:
```powershell
& $Py -m gnss_logger replay "$Run\disconnect.ubx" --json "$Run\disconnect_replay.json" --session-dir $Audit
$OldHash = (Get-FileHash "$Run\disconnect.ubx" -Algorithm SHA256).Hash
```
Подключите USB вручную, снова получите ports.json с НОВЫМ именем и при необходимости измените $Port:
```powershell
& $Py -m gnss_logger ports --format json --json "$Run\ports_reconnected.json" --session-dir $Audit
& $Py -m gnss_logger serial --port $Port --seconds 60 --inspect --capture "$Run\reconnected.ubx" --json "$Run\reconnected.json" --session-dir $Audit
$OldHash -ieq (Get-FileHash "$Run\disconnect.ubx" -Algorithm SHA256).Hash
```

Автоматического reconnect нет; это новый сеанс и новый UUID. Старые файлы не склеиваются.

## 4. Что передать для приёмки

Передайте весь $Run, exit codes и UTC-отметки действий. Даже неудачная попытка имеет
новый audit-каталог. Не переиспользуйте имена binary/report: при коллизии получите
сохранённую ошибку запуска без открытия COM. Ничего не удаляйте ради повторного теста —
создайте новый $Run.

Для 0.3.0 подтвердить отдельно: таймер, ошибки USB, Ctrl+C, фактические хеши partial-файлов,
наличие manifest/journal и сохранность входных файлов. Автономный PASS не заменяет этот retest.

