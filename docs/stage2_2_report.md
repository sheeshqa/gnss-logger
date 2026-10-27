# Stage 2.2 — первичная длительная регистрация

Дата: 2026-10-27. Версия: **0.4.0.dev2**.
**Автономная приёмка: PASS. Аппаратный record на Windows 10/Python 3.15.1: NOT TESTED.**
Версия установлена локально editable; pip check: No broken requirements found.
Stage 2.3 не начат. Git commit/push агентом не выполнялись.

## 1. Исходная база и аппаратные результаты пользователя

Stage 1 0.3.0 принят PASS WITH LIMITATIONS; Stage 2.1 0.4.0.dev1 принят автономно.
В существующем Git-проекте перед изменениями: main, b00166f, чистое рабочее дерево,
origin https://github.com/sheeshqa/gnss-logger.git. Новый Git не создавался, настройки не менялись.

Пользователь сообщил об успешном serial-тесте 0.4.0.dev1 на Windows 10 build 19045,
Python 3.15.1 x64, COM3/115200: 120 с, 120 RAWX при 1 Гц, 3230 SFRBX, 448200 байт,
0 пропущенных эпох/checksum/decode errors, matches_received_stream=true,
unarchived_bytes=0, registration=SUCCESS, exit=0, phase_quality=WARNINGS.
Этот результат принят как **сообщённый пользователем**: первичные файлы этого теста агенту
не предоставлены и независимо не проверялись. Он подтверждает прежний serial в сообщённой
среде и не переносится на новый record или X20P.

## 2. Реализованный pipeline

```text
record → Controller → explicit USB binding/open → one Reader thread
                         ↓ MON-VER poll in that same reader
                  bounded ByteQueue + one emergency returned block
                         ↓
                  main-thread PrimaryJournal → original bytes, no filtering
                         ↓
                  Diagnostics + SHA + checkpoints → final report → terminal manifest
```

- --seconds: монотонный таймер от старта контроллера, включая подготовку/идентификацию.
- --until-stopped: без таймера; Ctrl+C запрашивает управляемую остановку.
- Один reader владеет read/MON-VER/close соединения. Второго читателя для диагностики нет.
- Тайм-аут чтения ограничен оставшимся временем, обычно <=0.25 с; одинаковый timeout
  повторно не применяется, чтобы не вызывать ненужную реконфигурацию host serial.
- Дедлайн проверяется до нового read. Уже вернувшийся блок обрабатывается полностью.
- Очередь 8388608 байт по умолчанию, --queue-bytes настраивает вместимость в байтах.
- При overflow новые read прекращаются; последний вернувшийся блок удерживается отдельно
  (до 4096 байт физического reader) и дописывается после ранее принятой очереди.
  Дополнительно в памяти может находиться обрабатываемый writer-блок <=4096 байт.
  queue_peak_bytes отражает именно очередь, а не полную память Python/драйвера.
- --sync-interval 1 по умолчанию: flush/fsync первичного журнала и schema-v1 checkpoint
  подтверждённого префикса. Это целевой интервал, не real-time гарантия накопителя.
- Входные байты, включая noise/checksum errors/обрыв UBX-пакета, никогда не перекодируются,
  не дополняются и не прореживаются. Диагностика разбирает только записанный префикс.
- read-only allowlist Stage 1 сохранён. record посылает только MON-VER, не VALSET/RESET/
  включение RAWX/SFRBX/изменение частоты. stage1 serial/replay/mock/ports не менялись.

## 3. Контракты и происхождение

Protocol Controller.run возвращает diagnostic Mapping; CLI берёт код из его extensions.
Journal.append возвращает WriteReceipt; checkpoint/close имеют Mapping-результаты.
Контракты и опубликованные схемы Stage 2.1 v1 не изменены; новые runtime-поля только в
объявленных namespaced extensions. get_schema/validate_record проверяют каждый wire record.

Идентификация до записи не читает COM отдельным циклом: проверяются USB-атрибуты явно
выбранного порта, затем сохраняемый поток даёт реальный MON-VER. Тайм-аут --identity-timeout
начинается от poll, а не от подготовки каталога. Несовпадение модели/USB или отсутствие
ответа завершают попытку с ошибкой. Считанные до отказа байты сохраняются.
VID/PID/serial/location не угадываются, COM сам по себе недостаточен.
USB_TOPOLOGY при отсутствии serial — привязка к разъёму/модели, не доказательство физически
того же экземпляра. Автоматического поиска другого порта и reconnect нет.
Mock-сессии имеют simulated=true и MON-VER маркеры SIMULATED/MOCK, не реальные FW.

Все файлы создаются исключительно новыми под UUID run/attempt/connection/artifact:

```text
<output>/<run_id>/attempts/<attempt_id>/
  manifest.start.json          # начальный schema-v1 снимок
  primary/<connection>_<artifact>.ubx.part
  checkpoints/00000001.json    # synced offset, last sequence, prefix SHA
  events.jsonl                # schema-v1 события
  report.json                 # schema-v1 diagnostic; requires final manifest
  manifest.json               # ПОСЛЕДНЯЯ публикация, конечная точка финализации
```

После штатного закрытия primary публикуется как .ubx без замены существующего файла.
При source error/Ctrl+C/overflow/storage error он остаётся partial; при невозможности
независимой проверки — UNVERIFIED. JSON сначала записывается/синхронизируется под временным
именем, затем публикуется без замены. Неудавшаяся запись .tmp не является конечным manifest.
Чтение/замена существующих наблюдений ради сохранения новых запрещены.

Сначала закрывается/синхронизируется event log, затем публикуется report, последним manifest.
report имеет commit_manifest_required=true: без соответствующего конечного manifest
он предварительный. В частности, ошибка публикации manifest даёт stdout exit 1/PARTIAL,
а ранее записанный report не является доказательством завершения. При полном отказе диска
отчёт может остаться только в stdout; записать файлы на недоступный носитель нельзя гарантировать.

## 4. Учёт и остановка

```text
received = journal_written + pending + unarchived
journal_synced <= journal_written
journal_written = framed + opaque + unclassified
```

В финальном отчёте pending=0: очередь исчерпана либо остаток явно признан несохранённым.
written — подтверждённый FileIO.write префикс; actual_fingerprint независимо проверяет
реальный размер/хеш после закрытия. При расхождении файл UNVERIFIED/MISMATCH и exit !=0;
неподтверждённый префикс не объявляется восстановленным. received_count_exact=true относится
к вернувшимся Python-программе read, не к ещё буферизованным данным USB/приёмника.
После kill/потери питания runtime ничего не может сам дописать: это не recovery.

При исправном диске штатная остановка/ошибка источника/Ctrl+C сохраняют всю принятую очередь.
SIGINT handler выставляет только флаг, не выбрасывает исключение посреди записи/учёта/
финализации и не захватывает locks. Main дожидается reader, дописывает очередь и закрывает
журнал, независимо проверяет SHA-256, сохраняет последний read/data UTC и ошибки.

| Сценарий | Результат |
|---|---|
| Таймер/явный StopSignal, нормальный поток | 0 / STOPPED_COMPLETE / CLOSED_COMPLETE / VERIFIED |
| Ctrl+C после данных | 130 / PARTIAL / CLOSED_PARTIAL; сохранённый префикс проверен |
| COM/USB exception, queue overflow | 1 / PARTIAL либо FAILED; очередь по возможности сохранена |
| write/short write/flush/fsync/close failure | 1; сохранён принятый префикс, synced/unarchived/errors явно отражены |
| Нет RAWX/SFRBX | 2 / недостаточно наблюдений; это не аппаратная приёмка |
| Checksum/decode/gaps либо нет подтверждённой модели | Ненулевой код; данные и диагностика не исправляются |
| status/verify/recover | По-прежнему 4 / NOT_IMPLEMENTED, без I/O |

При пустых чтениях после пропадания потока финальная stale-проверка даёт ошибку вместо
фиктивного успеха: RAWX старше max(5 с,3/expected-hz), SFRBX старше 30 с. Раннее завершение
по stale/дetection/reconnect пока не реализовано: ожидание может продолжаться до таймера.
Фазовые WARNINGS не являются ошибкой сохранения или доказанными cycle slips.
CLOSED_COMPLETE означает полноту сохранения возвращённых байтов; unclassified может
содержать незавершённый UBX-хвост. Это не обещание целых сообщений/полных эфемерид.
Суточные файлы на границах сообщений появятся только в Stage 2.3.

## 5. Изменённые файлы

| Файл | Изменение |
|---|---|
| .gitignore | attempts/ исключает checkpoint/начальные manifest/остальные record-артефакты |
| gnss_logger/__init__.py, pyproject.toml | Версия 0.4.0.dev2, зависимости прежние |
| gnss_logger/recording/cli.py | Рабочий record, queue/sync/identity параметры; остальные stubs сохранены |
| gnss_logger/recording/controller.py | Жизненный цикл, reader/queue/writer, учёт/диагностика/финализация |
| gnss_logger/recording/reader.py | Явный COM/USB binding, один read loop, read-only MON-VER |
| gnss_logger/recording/storage.py | ByteQueue/emergency slot, первичный журнал и публикация метаданных |
| tests/test_stage2_record.py | 36 новых тестов runtime/faults/CLI/протоколов |
| tests/test_stage2_contracts.py | record исключён из проверки оставшихся NOT_IMPLEMENTED команд; его реальный CLI покрыт новыми тестами |
| tools/check_stage2_2_record.py | Повторяемая SIMULATED F9P/X20P CLI record + Replay без COM |
| README.md | Текущая функциональность/границы/ссылки |
| docs/stage2_1_contracts.md, docs/windows10_deployment.md | Исторические документы получили ссылки на актуальную версию |
| docs/stage2_2_hardware_test.md | Git/обновление/Python 3.15/30–60 минут/Ctrl+C/USB и критерии |
| docs/stage2_2_report.md | Этот отчёт |
| docs/evidence/stage2_2_tests.txt | Полный вывод итогового теста, исключён из Git |
| docs/evidence/stage2_2_simulated.json | Фактические синтетические результаты, исключены из Git |
| docs/evidence/stage2_2_checks.json | Сводка, исходные хеши/проверки, исключена из Git |

Всего 18 уникальных файлов; генерируемые editable egg-info/venv metadata не включаются
в Git. Stage 1 engine/sources/session/transport/requests/Diagnostics/PhaseTracker/UTCWriter
и адаптеры не менялись. Четыре schema JSON и recording/contracts.py также не менялись.

## 6. Фактические автономные проверки

Итоговый прогон в основном проекте: **147 tests, 0 failures, 0 errors, 0 skipped, OK**,
11.606 с. Это 111 существующих тестов и 36 новых; обновлён только смысл прежнего record-stub
подтеста согласно реализации команды. Синтаксис всего gnss_logger проверен грамматикой 3.9.
Команда: `.venv\Scripts\python.exe -B -m unittest discover -s tests -v`.
Полный вывод: [stage2_2_tests.txt](evidence/stage2_2_tests.txt).

Новые сценарии: timer-crossing read/empty read/partial UBX, bounded overflow/order,
source exception/close, Ctrl+C и idempotent signal handler, external StopSignal,
short/zero/failed write, flush/fsync/close faults, corrupted readback, checkpoint/event/report/
manifest publication failures, initial USB mismatch до open, MON-VER-only send, identity
timeout от poll, wrong model, X20P synthetic binding, exclusive creation/publication,
новый record CLI и прежний Replay. Ошибки диска искусственные, в отдельных временных файлах.

В полном наборе реально прошёл прежний большой UBX Replay: 9804 RAWX / 242168 SFRBX,
SHA-256 4e93d1672d8248bc310335d7636fa60e7b2c4b0231c9ebb114f5797cf86a809e.
Прежние serial тесты используют заглушки, не физические порты.

Отдельный конечный запуск tools/check_stage2_2_record.py с --seconds 2.2:

| Профиль | Байты | RAWX | SFRBX | Результат |
|---|---:|---:|---:|---|
| SIMULATED F9P | 354 | 2 | 2 | record/Replay 0, SHA/bytes/messages совпали, pending=unarchived=0 |
| SIMULATED X20P | 558 | 3 | 3 | record/Replay 0, SHA/bytes/messages совпали, pending=unarchived=0 |

Оба reader закрыты, каждый создал ровно один primary, stop_reason=duration,
received=written=synced=actual size. Различие числа эпох короткого синтетического окна
связано с подготовкой/первой выдачей и не выдается за пропуск физического наблюдения.
Полная сводка/пути: [stage2_2_simulated.json](evidence/stage2_2_simulated.json).
Все результаты SIMULATED отделены от аппаратных.

Установка -e . до 0.4.0.dev2 и pip check успешны. В аппаратной инструкции проверено
10 PowerShell блоков parser-ом без выполнения аппаратных команд. Git diff --check без
ошибок (есть обычные предупреждения autocrlf); отслеживаются только три MOCK UBX, не реальные.
attempts/.../checkpoints исключены git check-ignore. Никаких git add/commit/push агентом нет.

## 7. Python и аппаратная совместимость

| Среда | Проверено |
|---|---|
| Windows 11 / Python 3.12.14 x64 | 147 тестов и оба синтетических CLI record/Replay: PASS |
| Python 3.9 | Синтаксис AST 3.9: PASS; runtime NOT TESTED |
| Python 3.10–3.11 | Runtime NOT TESTED, интерпретаторы недоступны |
| Python 3.13–3.14 | Runtime NOT TESTED, интерпретаторы недоступны |
| Windows 10 19045 / Python 3.15.1 x64 | Прежний serial PASS по сообщению пользователя; новый record NOT TESTED |
| ZED-X20P реальный | NOT TESTED; отдельный адаптер сохранён, record проверен только синтетически |

Runtime-среды 3.9–3.15 не были скачаны/имитированы. Один домашний Python не выдаётся за
проверку всего диапазона; Python 3.15 требует полного автономного прогона на рабочем
ноутбуке до оборудования. Free-threaded сборки и другие ОС отдельно не проверены.

## 8. Сохранность и ограничения

SHA-256/размеры **77 защищённых файлов** проверены до/после: исходные Stage 1/hardware-retest
UBX/sidecar/логи, сохранённые Stage 2.1 отчёты/evidence и четыре JSON schema неизменны.
Реальные RINEX не редактировались и не перемещались. Полная проверочная сводка:
[stage2_2_checks.json](evidence/stage2_2_checks.json), локальная, исключена из Git.

Не добавлены UTC-ротация, reconnect, recovery, RINEX/TEC/ROTI, Drive или GUI.
Нет автоматического предотвращения сна, контроля свободного места/очистки или восстановления
после Windows restart/kill/потери питания. Checkpoint — подтверждение префикса, не готовая
система recovery. Пока создаётся примерно один checkpoint в секунду, что увеличивает число
метаданных при длительной работе; автоматического удаления нет.

Зависший системный read/write/fsync/close не может быть безопасно принудительно прерван
Python-кодом: немедленная остановка при аппаратном зависании не гарантируется. Обычный
serial read ограничен timeout; финализация при доступном накопителе проверена автономно.
Физические RTS/DTR-переходы драйвера, аварийное питание и 30–60 минут нового record
агентом не тестировались. Гарантия нулевой потери при отключении питания не заявляется.

## 9. Передача и решение

**Stage 2.2 автономно готов к аппаратной приёмке.**
Точные команды опубликования с домашнего компьютера, git pull --ff-only / pip install -e .
на рабочем ноутбуке, подготовка binding и записи --seconds 1800/3600 находятся в
[stage2_2_hardware_test.md](stage2_2_hardware_test.md).
Для Git-клона без личного лога ожидается 147 tests: 146 успешных + один ожидаемый skip.
Выполнить отдельно timer, Ctrl+C и USB-тесты, передать полный каталог результатов вне GitHub.

Критерий нормальной аппаратной приёмки: конечный schema-v1 manifest, exit 0,
STOPPED_COMPLETE/VERIFIED/CLOSED_COMPLETE, received=written=synced=actual size,
pending=unarchived=0, одинаковые SHA и Live/Replay counters, реальный F9P/1 Гц без
необъяснённых пропусков/ошибок. Фазовые предупреждения анализируются отдельно.
Публикация ожидает подтверждения пользователя. Stage 2.3 не начинается без отдельного согласования.
