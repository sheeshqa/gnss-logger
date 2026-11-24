# Завершение подготовки Application API

Дата: 2026-11-24. Версия: 0.5.0.dev2. Исходный HEAD: `67d26c5a6a70b1e434330faa9147a115ab8bf974`, ветка `main`.

## 1. Исходное состояние и границы работы

Прочитаны [архитектурный аудит](./gui_architecture_readiness.md), [предыдущий отчёт API](./gui_api_preparation_report.md), [известные проблемы диагностики](./known-diagnostics-issues.md), исходники и тесты. Уже существовали безопасные controller snapshots, ConversionJob/Cancel, DeviceRegistry, Settings/TOML и независимые auto-admission/Cancel. Повторно они не реализовывались.

Исходный полный прогон: **245 total, 242 PASS, 2 известных FAIL, 1 SKIP**, 75.789 с. Рабочее дерево до изменений содержало посторонние правки [device/ubx.py](../gnss_logger/device/ubx.py), [diagnostics.py](../gnss_logger/diagnostics.py), [test_diagnostics.py](../tests/test_diagnostics.py). Они сохранены без редактирования; диагностические сбои не исправлялись.

Выполнены A (сервисы/lifecycle), B (мониторинг/представление), C (история), D (интеграция), с отдельными тестами после подэтапов. Не создавался GUI, не устанавливались зависимости, не открывались реальные COM-порты, не выполнялись аппаратные тесты, команды изменения настроек, upload, commit/push. Алгоритмы UBX, parser, storage, verification, RINEX-профиль и научная обработка не изменялись. Предыдущие данные и отчёты не удалялись.

## 2. IMPORTANT-4–9: до и после

| Пункт | До | После | Реализованное решение |
| --- | --- | --- | --- |
| 4. Orchestration вне CLI | PARTIAL | READY | Нейтральный conversion.service с ConversionRequest; job/worker без CLI/argparse; recording service/handle; discovery и diagnose |
| 5. Signals/stdout | PARTIAL | READY | API не вызывает main, parser, print или signal wrappers; CLI сохраняет Ctrl+C и формат вывода |
| 6. Progress/errors | PARTIAL | READY | Pull-only OperationMonitor над существующими snapshots/Future; bounded transient queue и sticky critical/result |
| 7. Публичный Stop | PARTIAL | READY | RecordingHandle.stop() без аргумента причины, canonical stop_requested, повторный Stop безопасен, shutdown/result отдельно |
| 8. История | MISSING | READY | Read-only HistoryService, Stage 1/2, отчёты/receipts, provisional/missing/corrupt metadata, подтверждённые связи |
| 9. Раздельные статусы | PARTIAL | READY | present_result сохраняет registration, integrity, lifecycle, stop_reason, exit_code, phase, conversion/segments/files отдельно |

READY означает пригодность для первой версии GUI при соблюдении описанного асинхронного контракта. Это не аппаратная или научная сертификация.

## 3. Изменённые и добавленные файлы

### Новый нейтральный слой

- [application/__init__.py](../gnss_logger/application/__init__.py) — публичные экспорты без запуска операций при импорте.
- [application/recording.py](../gnss_logger/application/recording.py) — blocking run_recording и asynchronous RecordingHandle/start_recording; auto lifecycle и summary publication из прежнего CLI.
- [application/receivers.py](../gnss_logger/application/receivers.py) — discovery, DiagnosticRequest, read-only diagnose, общий result_code.
- [application/monitoring.py](../gnss_logger/application/monitoring.py) — согласованный pull-протокол событий без GUI callbacks.
- [application/results.py](../gnss_logger/application/results.py) — detached представление независимых характеристик результата.
- [application/history.py](../gnss_logger/application/history.py) — чтение истории и установление связей по metadata.
- [conversion/service.py](../gnss_logger/conversion/service.py) — перенесённое тело orchestration и frozen ConversionRequest; без новой реализации converter engine.

### Совместимые адаптации

- [conversion/cli.py](../gnss_logger/conversion/cli.py) — execute адаптирует прежний args в ConversionRequest; run печатает прежний JSON/возвращает прежний код.
- [conversion/job.py](../gnss_logger/conversion/job.py) — зависит от service, а не CLI; legacy args поддержан адаптером. Публичный operation_id известен до старта и совпадает с JSON операции.
- [conversion/worker.py](../gnss_logger/conversion/worker.py) — без Namespace/CLI; принимает программные Settings, предоставляет detached snapshot auto-очереди; cancel_current возвращает признак наличия текущей операции.
- [recording/cli.py](../gnss_logger/recording/cli.py) — вызывает recording service с внешним CLI-only run_with_signals.
- [cli.py](../gnss_logger/cli.py) — использует общий discovery/result_code. Legacy Stage 1 упаковка capture/session/JSON остаётся на консольной границе ради совместимости; GUI использует diagnose и не зависит от неё.

### Новые тесты

[configuration](../tests/test_application_configuration.py), [conversion](../tests/test_application_conversion.py), [recording](../tests/test_application_recording.py), [receivers](../tests/test_application_receivers.py), [monitoring](../tests/test_application_monitoring.py), [history](../tests/test_application_history.py), [integration](../tests/test_application_integration.py). Существующие assertions не ослаблялись и не изменялись ради зелёного результата.

## 4. Архитектура

```text
CLI adapters                         Future GUI / application worker
     |                                         |
     +----------- gnss_logger.application -----+
                   |
                   +-- discover_receivers / diagnose -> existing Probe/Diagnostics/run_source
                   +-- RecordingHandle / run_recording -> existing RecordController
                   |       +-- existing SegmentWorker (optional auto-conversion)
                   +-- ConversionRequest / service / existing ConversionJob -> existing converter engine
                   +-- OperationMonitor -> existing safe snapshots / Future / final reports
                   +-- present_result -> independent status dimensions
                   +-- HistoryService -> existing manifest/report/receipts

Device operations share existing DeviceRegistry -> owned serial access.
Filesystem publication, RINEX validation and cancellation cleanup stay in existing components.
```

Ни application services, ни conversion service/job/worker не импортируют CLI или argparse. Запись работает через тот же controller, serial reader и storage; конвертация — через тот же engine, RTKLIB и publication receipts. Отдельного recorder/converter для GUI нет.

## 5. Публичные контракты

Публичные имена доступны из [gnss_logger.application](../gnss_logger/application/__init__.py).

| Интерфейс | Сигнатура / результат |
| --- | --- |
| Discovery | `discover_receivers(inventory=None) -> list[dict]`; inventory supplier/iterable injectable; порт не открывается, модель остаётся NOT_QUERIED до MON-VER |
| Diagnostic request | `DiagnosticRequest(port, baud=115200, seconds=60.0, expected_hz=1.0, poll_mon_ver=True, inspect=False, request_timeout=2.0, binding=None)`; frozen/validated |
| Diagnostics | `diagnose(request, *, opener=open_device, registry=devices, inventory=None, source_factory=SerialSource, clock=monotonic, progress=None) -> dict`; blocking worker service |
| Recording service | `run_recording(settings, *, controller=None, stop=None, convert_config=None, conversion_settings=None, worker=None, runner=None) -> dict` |
| Recording handle | `RecordingHandle(settings, *, controller=None, convert_config=None, conversion_settings=None)`; `start()`, `stop()`, `snapshot()`, `result(wait=False, timeout=None)`, `shutdown(wait=False, timeout=None)` |
| Start shortcut | `start_recording(settings, **options) -> started RecordingHandle` |
| Auto control | `handle.set_auto_conversion(enabled: bool)`, `handle.cancel_conversion()`; Settings/config задаются при создании handle |
| Conversion request | `ConversionRequest(config=None, output=None, input=None, run_id=None, dry_run=False, allow_obs_without_nav=False)`; optional paths — Path, flags — bool |
| Blocking conversion | `run_conversion(request, sources=None, settings=None, cancel=None, progress=None, *, operation_id=None) -> dict` — alias service.execute |
| Conversion job | `ConversionJob(request=None, sources=None, *, settings=None, args=None)`; прежние `start/cancel/snapshot/result`; args только backward-compatible адаптер |
| Monitoring | `OperationMonitor(handle, operation_type, operation_id=None, capacity=8)`; `refresh()`, `poll()`, `drain()`, `critical`, `terminal`, `queued_count` |
| Result view | `present_result(report, operation_type='recording', conversions=(), lifecycle=None, artifacts=()) -> dict` |
| History | `HistoryService(recording_roots, conversion_roots, stage1_roots=())`; `list_sessions()`, `get_session(session_key)`, `list_conversions(session_key=None)`, detached `diagnostics` |

RecordSettings, DeviceBinding, ConversionSettings и load_conversion_settings — экспорты уже существующих контрактов. Для проверки/изменения Settings используется прежний TOML loader и dataclasses.replace.

### Запуск, Stop и ошибки

run_recording/diagnose/run_conversion блокируют только вызывающий worker. start() handle/job неблокирующий. result() по умолчанию возвращает None до завершения; wait/timeout — только ожидание, не команда отмены. Неожиданные исключения worker повторно выдаются через result; ошибки оборудования/диска проходят прежний structured report controller, operational diagnostic errors возвращаются в errors/exit_code. API не создаёт SystemExit и не настраивает stdout/stderr/signals.

Stop всегда `stop_requested`, не позволяет GUI выбрать произвольную причину. Запрос не означает завершение: STOPPING/drain и FINALIZING остаются видимыми в controller snapshots. Устройство удерживает прежний registry до конца финализации controller. Если auto-очередь ещё дренируется после завершения записи, handle остаётся FINALIZING, а registration записи уже может быть STOPPED_COMPLETE. Повторные stop безопасны; shutdown до старта не ждёт несуществующий поток; ошибка старта worker доступна через result.

Различаются Auto admission, ручной ConversionJob и Cancel текущего auto/manual задания. Disable auto не отменяет текущую или ранее принятую очередь. cancel_current/cancel_conversion возвращают False при отсутствии текущей auto-операции; job.cancel только выставляет Event. Прежние INTERRUPTED/130, OBS_ONLY_INCOMPLETE_NAVIGATION/3, SKIPPED/0 и публикация не менялись.

### События и медленный потребитель

Event envelope: `operation_id`, `operation_type`, `lifecycle`, `utc`, `sequence`, `kind`, `data`. ID стабилен за жизнь monitor; для handle/job берётся их публичный operation_id, для Future можно передать свой ID. Raw run/attempt/conversion IDs не переписываются.

Monitor — pull adapter: **не создаёт потоков и не вызывает UI callbacks**. refresh читает безопасный snapshot и nonblocking result либо проверяет Future.done перед result. poll/drain читают уже подготовленные события. Промежуточные state/progress coalesced; очередь не больше capacity плюс два sticky slots — critical и terminal. terminal/critical доступны повторно даже после drain.

Critical представляет **последний cumulative error snapshot** источника, errors_total и API/auto conversion error, а не бесконечный журнал отдельных ошибок. Переполнение progress не вытесняет ошибки или terminal result; при последующем исключении источника сохраняются предыдущие errors и данные о registration/опубликованных сегментах. Существующие source limits на сохранение текста ошибок не снимаются; monitor не может восстановить тексты, которые источник не сохранил. Warnings и независимые качества доступны в raw progress/terminal presentation; процентный прогресс RTKLIB не выдумывается.

Все возвращаемые snapshots/events/result views — глубокие копии. Память буфера не растёт от длительности записи или числа refresh; это не обещание фиксированного размера всей диагностики ядра или произвольно большого конечного отчёта. Для тяжёлых снимков refresh лучше выполнять в polling worker, а GUI передавать события через его собственный event loop.

### Раздельные статусы

View содержит отдельно lifecycle, registration, integrity, stop_reason, exit_code, warnings, phase_quality, conversion/conversions, segments, published_files, artifacts, diagnostics и errors. Для unexpected monitor-source failure дополнительно api_error, без затирания последнего безопасного результата.

Проверены все девять требуемых сочетаний: успешная запись без конвертации / с успехом / отменой / ошибкой конвертации; OBS_ONLY с файлами и code 3; SKIPPED/0 без RINEX; штатная запись с предупреждениями; hardware partial; сохранённые ранее сегменты после Cancel. Результат записи не меняется из-за ошибки независимой конвертации. NO_INDICATORS_DETECTED не классифицируется как предупреждение или научный PASS; PhaseTracker WARNINGS отражается отдельно.

## 6. Примеры использования

Код ниже — примеры для будущего приложения, в этой задаче на приёмнике не выполнялись. Блокирующие вызовы и ожидания должны выполняться вне UI thread.

```python
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from gnss_logger import application as api

executor = ThreadPoolExecutor(max_workers=3)

# Discovery можно вызвать в worker; никаких выводов о модели по названию порта.
ports_future = executor.submit(api.discover_receivers)
# После завершения взять ports_future.result(), выбрать порт явным действием.

request = api.DiagnosticRequest(selected_port, inspect=True, binding=binding)
diagnostic_future = executor.submit(api.diagnose, request)
diagnostic_monitor = api.OperationMonitor(diagnostic_future, 'diagnostic')
# По завершении identity содержит фактически наблюдённый MON-VER.

record_settings = api.RecordSettings(
    output=Path(primary_directory).resolve(), initial_port=selected_port,
    binding=binding, seconds=None, daily_root=Path(daily_directory).resolve())
conversion_settings = api.load_conversion_settings(Path(config_path).resolve())
form_values = asdict(conversion_settings)
edited = replace(conversion_settings, timeout_s=1800)  # прежняя валидация

handle = api.RecordingHandle(record_settings, conversion_settings=edited)
handle.set_auto_conversion(False)   # auto выключен, ручная конвертация независима
handle.start()
record_monitor = api.OperationMonitor(handle, 'recording', capacity=8)

view = handle.snapshot()            # последний owner-published snapshot + auto state
record_monitor.refresh()            # pull; удобно из polling worker
events = record_monitor.drain()     # передать в event loop GUI
handle.set_auto_conversion(True)    # принимать будущие закрытые сегменты
handle.cancel_conversion()         # отдельно отменить текущую auto-конвертацию
handle.stop()                      # canonical stop_requested, повторять безопасно
recording_report = handle.result()  # None до полного завершения
# handle.result(wait=True) / executor.shutdown(wait=True) — только вне UI thread.

# Ручная конвертация; исходник выбирается только после завершения UBX.
conversion_request = api.ConversionRequest(input=Path(completed_ubx).resolve())
job = api.ConversionJob(conversion_request, settings=edited).start()
conversion_monitor = api.OperationMonitor(job, 'conversion')
job.cancel()                       # отдельное действие пользователя
conversion_report = job.result()   # None пока выполняется; после Cancel INTERRUPTED/130

# Когда оба отчёта доступны, без пересчёта правил publication:
if recording_report is not None and conversion_report is not None:
    outcome = api.present_result(recording_report, conversions=[conversion_report])
    files = outcome['published_files']  # пути, не ключи obs/nav

# Read-only history — также worker, если каталог большой.
history = api.HistoryService(
    recording_roots=[Path(runs_root)], conversion_roots=[Path(rinex_root)],
    stage1_roots=[Path(old_sessions_root)])
sessions = history.list_sessions()
if sessions:
    detail = history.get_session(sessions[0]['session_key'])
    conversions = history.list_conversions(detail['session_key'])
metadata_issues = history.diagnostics
```

binding — существующий DeviceBinding с моделью и VID/PID, ровно одним USB serial/location. При MON-VER его модель проверяется существующим ядром; нельзя считать USB description доказательством модели. Диагностика и запись используют один registry; диагностику нельзя параллельно открывать на занятом записью приёмнике. Разные устройства независимы.

Изменение Settings создаёт новый объект и не меняет текущий job. Согласованные systems/groups/masks проверяет существующая валидация; автоматического добавления сигналов нет. Сохранение отредактированного TOML не реализовывалось. Programmatic Settings фиксируются в report.settings; историческое config_path может обозначать путь из запроса/default, а не файл, реально прочитанный при override.

## 7. Read-only история

Поддержаны Stage 1 manifest v1/report v1–2 без schema_id, Stage 2 manifest/diagnostic v1–2, projection artifact metadata, conversion operation v2 и result receipt v1. Проверяются типы/версии, вложенные структуры и ownership. Неверные/нечитаемые metadata попадают в diagnostics, не прерывая весь каталог.

Сводка сессии содержит session_key, IDs, model/identity, started/ended, duration_s, registration/state, stop_reason/exit_code, recorded_integrity, warnings/errors, paths, artifacts/ubx_files и связанные conversions. Терминальный manifest — authority завершения. При одном report без валидного manifest данные provisional, state INCOMPLETE, завершение не объявляется подтверждённым. Отсутствующий referenced file имеет present=False независимо от старого статуса.

Связи устанавливаются по run/attempt/artifact IDs с проверкой ownership или сохранённой паре SHA-256/bytes, **без чтения содержимого UBX ради нового hash**. Противоречия IDs/fingerprint не игнорируются, похожие имена не используются. Неоднозначный match остаётся AMBIGUOUS с candidates, а standalone conversion остаётся UNLINKED. Копии с одинаковым ID не выбираются произвольно; get_session с неоднозначным bare ID требует точный session_key.

Receipts возвращаются с `kind='receipt'`, отчёты запусков — `kind='operation'`: это не два независимых запуска одной конвертации. Метаданные не мигрируются, не восстанавливаются, файлы не удаляются/не перемещаются. recorded_integrity — сохранённое решение прошлого запуска; present — текущий stat; independently_verified=False — новая проверка содержимого не выполнялась. Публичные запросы одного HistoryService сериализованы RLock, results/diagnostics detached.

## 8. Результаты тестов и совместимость CLI

Интерпретатор итоговых прогонов: существующий `C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe`, Python 3.15.1. Синтетические readers/MockReceiver, DI, временные каталоги и управляемые собственные Python subprocesses; реальный COM и RTKLIB hardware workflow не использовались.

| Проверка | Итог |
| --- | --- |
| Исходный полный baseline | 245 total: 242 PASS, 2 known FAIL, 1 SKIP |
| Новые Application API тесты | **60/60 PASS**, 10.306 с |
| В том числе conversion / receivers / recording / configuration | 7 / 10 / 4 / 2 PASS |
| В том числе monitoring / history / integration | 23 / 12 / 2 PASS |
| Существующие Stage 1 fixes (промежуточно) | 26/26 PASS |
| Существующие recording tests (промежуточно) | 36/36 PASS |
| Конвертация с новыми request tests (промежуточно) | 57/57 PASS |
| **Финальный полный набор** | **305 total: 302 PASS, 2 known FAIL, 1 SKIP, 0 errors**, 49.759 с |

Прирост относительно baseline — 60 успешных тестов, без новых регрессий. Неудачные тесты прежние: `test_all_chunk_boundaries_and_mixed_stream` (14 != 0), `test_sfrbx_length_mismatch_is_not_counted` (префикс 02-13). Пропущен прежний saved-user-log regression: локальный UBX не распространяется. Полный suite возвращает exit 1 из-за двух известных FAIL; он не объявляется полностью зелёным.

```powershell
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -p 'test_application_*.py' -q
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -q
```

Проверены direct services из worker без print/signals, повторный Stop, source/disk error semantics, registry busy/independent receivers, nonblocking Future monitoring, coalescing/overflow/slow consumer, sticky terminal/errors, отмена и сохранение published outputs, TOML overrides включая auto Settings, malformed/provisional history и ambiguity. Сквозной тест использует публичный application пакет для discovery → diagnose → recording → conversion → history, не main/Namespace/stdout.

Одна промежуточная регрессия новой проверки ID была установлена и устранена: validator ошибочно применялся и к подменённому в тесте UUID-генератору. Проверяется явно переданный operation_id; прежний путь UUID-генерации и assertions сохранены. Также до продолжения исправлены ошибки нового presentation adapter (ключи вместо путей и сохранение предыдущего результата при source exception).

CLI-команды, параметры, JSON и exit codes сохранены; прежние compatibility/cancellation/worker интеграции проходят. Ctrl+C остаётся CLI-wrapper. CLI status/verify/recover остаются заглушками, новые сервисы истории не выдаются за реализацию этих команд. Проверка редактора для production модулей без ошибок; git diff --check без замечаний. Тесты создали новые игнорируемые [audit outputs](../reports/sessions/), не перезаписывая предыдущие результаты.

## 9. Известные ограничения

- Blocking service/history calls и ожидания result/join/finish должны выполняться в workers. Библиотека не выбирает GUI event loop и не гарантирует hard-real-time задержки при storage IO.
- Monitor pull-only: приложение организует refresh/timer. Sticky critical/result не является бесконечным event journal; warnings доступны в представлении результата, source error caps остаются прежними.
- Автоматическая очередь даёт bounded state/counters, last report/error; подробные законченные conversion reports доступны через HistoryService. Процентного convbin progress нет. Disabled auto не подбирает задним числом пропущенные сегменты; ручной запуск отдельный.
- Каждый history query перечитывает metadata; это не атомарный снимок внешне изменяемой файловой системы. Большие каталоги читать в worker. Новая независимая верификация SHA не включена в обычный запрос.
- Stage 1/2 имеют разные схемы: недостающие сведения возвращаются как unknown/None/provisional, не угадываются. Критически повреждённые документы могут не дать идентифицируемую сессию, но остаются в diagnostics.
- Registry внутрипроцессный. Внешние u-center/другие процессы обрабатываются прежними ошибками открытия. Нельзя обходить owned access через raw transport. Автоматический reconnect и перенос identity между неизвестными USB topology не добавлены.
- Cancellation и publication сохраняют ограничения предыдущего этапа: commit не прерывается посередине, файловые проверки кооперативны, завершение child/cleanup не обещает общего hard timeout.
- Программная диагностика не создаёт автоматически CLI audit session на диске: возвращает структурированный результат; legacy CLI сохраняет свои отчёты как раньше.
- Проверена текущая среда Python/Windows, не вся поддерживаемая матрица версий; аппаратная приёмка и научные выводы не входят в эту работу.

## 10. Готовность и следующие действия

| Вопрос | Ответ |
| --- | --- |
| GUI без CLI main/argparse? | **Да**, через публичные requests/services/handles |
| GUI без разбора stdout? | **Да**, structured results/snapshots/events/history |
| Основные операции из фоновых потоков? | **Да**, при соблюдении nonblocking UI и общего DeviceRegistry |
| Достаточно ли API для первой версии GUI? | **Да**, включая выбор/идентификацию, Start/Stop, мониторинг, ручную/auto-конвертацию, Cancel и историю |
| Остались ли архитектурные BLOCKER/IMPORTANT для такого MVP? | **Не выявлены в проверенном объёме**; перечисленные эксплуатационные ограничения сохраняются |

Следующие задачи — GUI-specific: выбрать совместимый framework отдельным решением, сделать mock-first UI, marshaling в UI thread, закрытие окна с async shutdown, отображение independent status dimensions и файлов. Отдельно остаются два baseline диагностических дефекта, GUI acceptance tests и будущая явно разрешённая аппаратная проверка. Редактор/сохранение TOML, поиск/пагинация больших архивов и новые CLI status/verify/recover — самостоятельные задачи, не скрытые требования текущего API.

Разработка GUI не начата. Работа остановлена после реализации, тестирования и этого отчёта; commit/push не выполнялись.
