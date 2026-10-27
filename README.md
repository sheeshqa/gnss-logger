# GNSS Logger 0.4.0.dev2 — первичная регистрация Stage 2.2

Read-only CLI для ZED-F9P / ZED-X20P: Serial, Replay и Mock. GUI отсутствует.
**Stage 1 0.3.0 принят пользователем после аппаратного ретеста PASS WITH LIMITATIONS.**
Новый `record` реализует таймер/непрерывный режим, один COM reader, ограниченную очередь,
первичный журнал всех возвращённых байтов и финализацию с SHA-256/учётом/manifest.
По умолчанию очередь 8 МиБ, sync 1 с. Нужны явные USB binding и модель; посылается только MON-VER.
`status`, `verify`, `recover` остаются **4 / NOT_IMPLEMENTED**. UTC-ротации/reconnect/recovery пока нет.
Версия 0.4.0.dev2 аппаратно **NOT TESTED**. Сообщённый пользователем smoke на Win10/Python 3.15.1
относится к прежнему `serial` 0.4.0.dev1, а не к новому record.
Существующие команды Stage 1 работают как прежде.
Описание контрактов: [stage2_1_contracts.md](docs/stage2_1_contracts.md).
Отчёт подэтапа: [stage2_1_report.md](docs/stage2_1_report.md).
Stage 2.2: [отчёт](docs/stage2_2_report.md), [обновление и аппаратный тест](docs/stage2_2_hardware_test.md).
Автономный запуск реального CLI record с явно синтетическим источником без COM:
`python tools/check_stage2_2_record.py --output <NEW_SIMULATED_DIRECTORY>`.
Результаты старой версии и сообщение пользователя о RINEX/TEC сохранены отдельно.
Актуально: [отчёт об исправлениях](docs/stage1_fixes_report.md) и
[короткий повторный аппаратный тест](docs/stage1_retest.md).
Итог и фактический анализ пользовательского лога: [stage1_report.md](stage1_report.md).

Инструкция установки и обновления на другом ноутбуке: [Windows 10 deployment](docs/windows10_deployment.md).
Личные аппаратные отчёты и docs/evidence остаются локальными и не входят в Git-перенос.
Для конфигурации переносится нейтральный config/station.template.json.

## Быстрый запуск в существующем окружении

В папке проекта уже установлено `.venv` на доступном агенту Python 3.12.14, pySerial 3.5.

```powershell
# Выполнять из корня вашего Git-клона GNSS Logger
.\.venv\Scripts\python.exe -m gnss_logger --help
# Полный пользовательский файл без задержек, без обращения к COM.
.\.venv\Scripts\python.exe -m gnss_logger replay 'data\COM4___115200_260305_024016.ubx'
# Воспроизведение с ускорением по RAWX-эпохам.
.\.venv\Scripts\python.exe -m gnss_logger replay 'data\COM4___115200_260305_024016.ubx' --speed 1000
# Явно синтетические приёмники с виртуальными часами.
.\.venv\Scripts\python.exe -m gnss_logger mock --profile f9p
.\.venv\Scripts\python.exe -m gnss_logger mock --profile x20p --scenario partial
.\.venv\Scripts\python.exe -m gnss_logger mock --profile f9p --scenario timeout
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$LASTEXITCODE
```

Для первой установки из исходников нужен Python 3.9+ с venv/pip (укажите полный путь,
если `py` отсутствует):

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Код проверен синтаксически для Python 3.9; выполнение на Windows 10/Python 3.9.13
и Python 3.13.14 ещё не тестировалось. Это не готовый автономный exe.
Будущая сборка с собственным runtime допустима и не зависит от системного Python ноутбука.

## Replay и запись по UTC

`--speed 0` — без задержек (по умолчанию), `1` — реальное время эпох, `N` — ускорение ×N.
Это воспроизведение времени измерений, не восстановление неизвестных USB-задержек между пакетами.
Не-RAWX сообщения идут в исходном порядке рядом с последней известной эпохой.

```powershell
New-Item -ItemType Directory -Force reports, data | Out-Null
.\.venv\Scripts\python.exe -m gnss_logger replay 'data\COM4___115200_260305_024016.ubx' --capture data\new_copy.ubx --record-dir data\new_segments --json reports\new_replay.json
.\.venv\Scripts\python.exe -m gnss_logger replay tests\fixtures\MOCK_midnight.ubx --speed 1 --record-dir data\midnight_segments --json reports\midnight.json
```

Capture сохраняет каждый исходный байт, включая запросы в историческом логе, шум и обрывки.
Ничего из Replay не отправляется на порт. Новые файлы создаются эксклюзивно, без перезаписи.
Сегменты получают уникальный session ID; для mock имена начинаются с `MOCK_`.
Соседний `.meta.json` сохраняет известное происхождение данных, которое переносится при Replay.
Фикстуры с `MOCK` и их NAV — синтетика, непригодная для научной обработки.

UTC Replay выводится из RAWX week/TOW/leapS только при валидном leapS. Это время приёмника,
не независимая поверка часов. Если время неизвестно, для записи нужен явный UTC-якорь:

```powershell
.\.venv\Scripts\python.exe -m gnss_logger replay some_log.ubx --start-utc 2026-03-05T00:00:00Z --record-dir data\anchored
```

`--expected-hz 1` задаёт ожидание диагностики, не меняет приёмник и не прореживает данные.
Для фактических эпох через 30 секунд можно указать `--expected-hz 0.03333333333333333`.
Пропуски фиксируются с временем и длительностью; никакие измерения не вставляются.

## Mock и запросы

Сценарии: `normal`, `partial`, `timeout`, `nak`, `ack`, `ack-only`, `corrupt`, `malformed`, `no-data`.
По умолчанию имитируются 5 эпох, MON-VER и чтение трёх RAM-ключей. Реальной задержки нет:
тайм-ауты проверяются по виртуальным часам. `--epochs` меняет число тестовых эпох.
Протокольные версии в фикстурах явно обозначены `MOCK`, а не выдаются за установленную FW.

## Serial — только после аппаратного checklist

[План аппаратных проверок](docs/hardware-test.md) полностью имеет статус NOT TESTED.
`ports` только перечисляет доступные порты. Модель не определяется по номеру COM.
«Другой владелец порта» — u-center, терминал или другой процесс, уже открывший тот же COM.

```powershell
.\.venv\Scripts\python.exe -m gnss_logger ports
# Пассивное чтение: никаких UBX-запросов.
.\.venv\Scripts\python.exe -m gnss_logger serial --port COM5 --baud 115200 --seconds 60
# Только MON-VER; затем не меняем поток сообщений приёмника.
.\.venv\Scripts\python.exe -m gnss_logger serial --port COM5 --seconds 60 --poll-mon-ver
# MON-VER, затем read-only RAM: RATE-MEAS + USB RAWX/SFRBX.
.\.venv\Scripts\python.exe -m gnss_logger serial --port COM5 --seconds 60 --inspect --json reports\new_serial.json
```

COM5 — пример; выберите фактический COM. При отсутствии устройства — понятная ошибка и код 1,
без traceback. Автоматический reconnect ещё не реализован. `diagnose` — совместимый псевдоним `serial`.
Закройте u-center перед открытием COM. POWER+GPS simpleRTK2B — native USB;
baud на компьютере не является командой изменения скорости приёмника.

Flow control отключён, RTS/DTR заданы False до open и не переключаются во время сеанса.
RESET/BOOT не используются. Поведение драйвера при open/close на реальной плате не проверено.
В Windows эксклюзивное открытие обеспечивает pySerial/Win32; программные тесты используют заглушки.

## Отчёты и коды выхода

JSON выводится в stdout, `--json` сохраняет новый отчёт; краткий статус каждые 5 с идёт в stderr.
В отчёте отдельно указаны mode, simulated, provenance и hardware_test_status.
Хеш всех полученных байтов и фактического capture-файла сохраняется также при ошибке/прерывании;
partial-файл дополнительно можно проверить Replay. Ctrl+C закрывает порт и файлы (код 130).

| Код | Значение |
|---|---|
| 0 | RAWX/SFRBX и валидные code/phase присутствуют; это не аппаратная приёмка и не TEC-допуск |
| 1 | Ошибка I/O, COM, пути, записи или метаданных |
| 2 | Недостаточно наблюдений, включая пустой поток |
| 3 | Ошибки/пробелы/устаревшие сообщения или неуспешный запрос |
| 130 | Остановка оператором |

Нет CFG-VALSET/VALDEL/RESET и произвольной отправки команд. `--inspect` читает только разрешённый
поднабор RAM на USB, неизвестная модель не получает запросы конфигурации.
Snapshot из Replay — исторические страницы, не текущая конфигурация устройства.
Значения VALGET сохраняются как unsigned представления битов; общая типизация signed/enum
для всех возможных ключей пока отсутствует.

Ротация Serial пока по host arrival UTC и не гарантирует разбиение целыми UBX-пакетами;
научное распределение эпох и полноценная длительная эксплуатация относятся к Stage 2.
Нет RINEX/convbin/Drive/GUI, автоудаления, disk guard, восстановления после питания и защиты от сна.
Полные ограничения, источники, результаты: [отчёт Stage 1](stage1_report.md),
[архитектура](docs/architecture.md), [протокол](docs/protocol.md),
[принятые ответы](docs/user-context.md), [ADR](docs/adr-001.md).

## Изменения 0.3.0

Каждый запуск serial/replay/mock/ports получает UUID и новый каталог аудита:
- с --session-dir DIR: внутри DIR;
- с --json path/report.json без --session-dir: внутри path/sessions;
- иначе: reports/sessions относительно текущего рабочего каталога.

В каталоге находятся report.json, manifest.json и events.jsonl (структурированный журнал,
включая ошибки запуска и прогресс). --json остаётся дополнительным явным отчётом;
повторное имя не перезаписывается, неудачная попытка получает собственный audit-каталог.

Новые поля: run (хеш всех возвращённых источником байтов, read_calls, empty_reads, последние
UTC чтения, причина остановки), artifacts (хеш/размер фактического файла, состояние, complete,
matches_received_stream, unarchived_bytes), registration и независимый phase_quality.
При ошибке/Ctrl+C binary остаётся partial, с manifest и sidecar; ошибки flush/fsync/close
отмечаются. Полнота файла означает сохранение полученных байтов в завершённом сеансе,
а не полноту спутниковых наблюдений или отсутствие фазовых проблем.

phase_quality = NO_DATA / WARNINGS / NO_INDICATORS_DETECTED. Последнее значение
не доказывает отсутствие cycle slips или научную пригодность.
Для сравнения с 0.2.0 используйте исходные байты, хеши, messages, epochs, signals, валидность
измерений и timing; расширенный учёт locktime после невалидных/пропавших наблюдений
может обнаружить больше индикаторов. Измерения не меняются.

Для Unicode-списка портов используйте:
```powershell
.\.venv\Scripts\python.exe -m gnss_logger ports --format json --json reports\ports_new.json
```
Сначала создайте reports. VID 1546 — подсказка u-blox USB, не определение F9P/X20P.
Модель по-прежнему определяется по MON-VER, номер COM вручную выбирается заново.
