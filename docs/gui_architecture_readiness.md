# Готовность CLI и API к Windows GUI

Дата аудита: 2026-11-23. Версия: 0.5.0.dev2. HEAD: `6c9f2341c506a913b360918a897cd060ec2f2eeb`.

## Область и метод проверки

Проверены фактические реализации, сигнатуры, вызывающие функции и тесты, а не только имена модулей. Выводы относятся к текущему рабочему дереву: до аудита уже изменены [device/ubx.py](../gnss_logger/device/ubx.py), [diagnostics.py](../gnss_logger/diagnostics.py) и [test_diagnostics.py](../tests/test_diagnostics.py). Эти изменения сохранены без вмешательства. Локальные HEAD и origin/main совпадали; отдельная проверка GitHub в аудит не входила.

Создан только этот документ. Порты не перечислялись и не открывались; аппаратная запись, команды приёмнику, установка зависимостей, изменение кода, commit и push не выполнялись. Автотесты используют синтетические источники, временные каталоги и подмену оборудования; отдельные тесты запускают собственные безопасные дочерние Python-процессы. Исторический [отчёт восстановления OBS](../../../reports/x20p_obs_recovery_20261120.md) прочитан как контекст, а не как новый аппаратный результат. TEC/ROTI вне области аудита.

## A. Текущая архитектура

### Точки входа и зависимости

Консольный entry point `gnss-logger = gnss_logger.cli:main` задан в [pyproject.toml](../pyproject.toml). Запуск `python -m gnss_logger` через [__main__.py](../gnss_logger/__main__.py) вызывает тот же `main(argv=None)` и только на внешней границе делает `SystemExit`.

```text
CLI main(argv)
 ├─ ports / diagnose(serial) / replay / mock
 │    └─ CLI orchestration + Session + run_source + Diagnostics + Probe
 │         └─ SerialSource / ReplaySource / MockReceiver
 ├─ record → recording.cli.preview → RecordController.run
 │    ├─ SerialReader → transport → pySerial
 │    ├─ ByteQueue → PrimaryJournal → Metadata / manifest
 │    └─ Projection → daily artifacts + Verifier
 │         └─ optional on_segment → SegmentWorker → conversion.cli.execute
 ├─ convert → conversion.cli.run → execute
 │    └─ select → stage_report → convert → invoke(convbin)
 │         └─ snapshot / RINEX validators / results.commit
 └─ status / verify / recover → NOT_IMPLEMENTED (exit 4)
```

| Область | Реальные компоненты и роль |
| --- | --- |
| CLI | [cli.py](../gnss_logger/cli.py): argparse, dispatch, Stage 1 orchestration, вывод JSON/прогресса; [recording/cli.py](../gnss_logger/recording/cli.py): настройки, signal wrapper, worker; [conversion/cli.py](../gnss_logger/conversion/cli.py): операция, агрегация, публикация отчёта |
| Запись | [RecordController](../gnss_logger/recording/controller.py), неизменяемые [RecordSettings / DeviceBinding / State](../gnss_logger/recording/contracts.py), [SerialReader / make_reader](../gnss_logger/recording/reader.py) |
| Устройство | [port_info](../gnss_logger/device/ports.py) только нормализует USB-описание; перечисление `comports()` выполняет CLI; [transport](../gnss_logger/device/transport.py) открывает порт; [Probe](../gnss_logger/device/requests.py) ведёт MON-VER/ограниченный VALGET; [adapters](../gnss_logger/device/adapters/) различают модели |
| Декодирование и диагностика | [Diagnostics.feed(data) / report()](../gnss_logger/diagnostics.py), [UBXParser](../gnss_logger/device/ubx.py), [PhaseTracker](../gnss_logger/phase.py); счётчики, идентичность, качество, не коррекция измерений |
| Хранение записи | [PrimaryJournal / Metadata / publish_exclusive](../gnss_logger/recording/storage.py), [Projection](../gnss_logger/recording/projection.py), [indexes](../gnss_logger/recording/indexes.py), [Verifier](../gnss_logger/recording/verification.py), [schema](../gnss_logger/recording/schema.py) / [schema_v2](../gnss_logger/recording/schema_v2.py), [capture/names](../gnss_logger/capture/names.py) |
| Старые сессии / capture | [Session / CaptureFile / file_fingerprint / write_new_json](../gnss_logger/session.py), [run_source / RunState](../gnss_logger/engine.py), [UTCWriter](../gnss_logger/capture/writer.py). Это отдельный Stage 1 путь, не замена современному controller |
| Конвертация | [Settings / load](../gnss_logger/conversion/config.py), [Source / select](../gnss_logger/conversion/selection.py), [engine](../gnss_logger/conversion/engine.py), [observations / navigation](../gnss_logger/conversion/rinex.py), [results](../gnss_logger/conversion/results.py), [SegmentWorker](../gnss_logger/conversion/worker.py) |

Направление основной зависимости удачное: controller не импортирует argparse/CLI; converter engine также не читает sys.argv. Но фоновой [SegmentWorker](../gnss_logger/conversion/worker.py) импортирует `execute` из CLI-модуля и создаёт `Namespace`: orchestration ещё не полностью отделена.

### Что уже повторно используется без переписывания алгоритмов

Можно сохранить [RecordController.run / request_stop](../gnss_logger/recording/controller.py), контракты настроек, reader/journal/projection, валидаторы, fingerprints, [Diagnostics / Probe](../gnss_logger/diagnostics.py), [Source / select](../gnss_logger/conversion/selection.py), [stage_report / convert / probe](../gnss_logger/conversion/engine.py), правила публикации и [conversion.execute](../gnss_logger/conversion/cli.py). Последняя функция доступна из Python уже сейчас, но её CLI-образная форма — временная интеграционная граница, не окончательный Application API. Конкурентный вызов живого `snapshot()` пока не следует считать безопасным.

### Отчёты и логирование

Есть структурированные JSON-отчёты, JSONL-события, checkpoints и fingerprints. [Metadata](../gnss_logger/recording/storage.py) валидирует записи, flush событий и fsync при закрытии; controller публикует terminal manifest последним. [Session](../gnss_logger/session.py) имеет другую историческую схему и порядок завершения — нельзя смешивать их семантику.

Общего logging/event API для интерфейсов не найдено: Stage 1 пишет progress через `print(..., stderr)`, recording events идут в Metadata, `convbin` stdout/stderr сохраняются в process-полях (последние 65536 байт каждого потока с флагом усечения). GUI может читать готовые отчёты, но для live UI нужен отдельный поток структурированных уведомлений, не разбор консольных строк.

## B. Готовность API записи

READY — применимо напрямую в оговорённом контексте; PARTIAL — нужна адаптация; MISSING — соответствующего интерфейса нет.

| Возможность | Оценка | Факт и ограничения |
| --- | --- | --- |
| Программный запуск | READY | `RecordController(...).run(settings, stop=None) -> Mapping`; возвращает диагностический dict, не `(code, report)`. Код в `extensions['gnss_logger.recording']['exit_code']` |
| Отдельный controller | READY | `RecordController` — single-use; повторный run отклоняется. Создавать новый экземпляр на сессию |
| Параметры | READY | `RecordSettings(output: Path, initial_port: str, binding: DeviceBinding, seconds: Optional[float], baud=115200, expected_hz=1.0, station='UBX1', retention_policy='keep_all', daily_root=None)`; `seconds=None` — до остановки. Конструктор controller отдельно принимает queue_bytes, sync_interval, identity_timeout и фабрики |
| Выбор устройства | PARTIAL | USB VID/PID + ровно один serial/location обязательны; модель подтверждается входящим MON-VER. `port_info(port) -> dict` не обнаруживает модель. Полного самостоятельного сервиса inventory + identify нет |
| Безопасная остановка | READY | Внешний Event с `is_set()` или `request_stop(reason)`; producer учитывает уже возвращённый блок, закрывает reader, queue дренируется, выполняется финализация |
| Причина пользовательской остановки | PARTIAL | Использовать `stop.set()` либо `request_stop('stop_requested')`: произвольная причина вроде `user_stop` не входит в allowlist complete в `PrimaryJournal.close`, поэтому даст partial artifact. Отдельного стабильного enum причин нет |
| Состояние и прогресс | PARTIAL | `snapshot() -> dict`: State, accounting, last data/RAWX/SFRBX/sync, gaps/errors, extensions Diagnostics с RAWX epochs/SFRBX. До run `self.d is None`; snapshot не поддерживается. Нет документированной согласованности во время другого потока |
| Ошибки USB / порта | READY | Открытие/идентификация/чтение/закрытие дают структурированные errors и stop_reason; отключение прекращает сессию, reconnect отсутствует |
| Ошибки диска | READY | Short write, flush/fsync/close/readback/publication отражаются в accounting, integrity и manifest; сохранённый префикс не выдаётся за полную запись |
| Фоновый запуск | PARTIAL | run блокирует вызывающий поток; внутри есть отдельный producer, но caller продолжает обработку/запись. Весь run нужно выполнять в worker, не в GUI thread |
| Завершение файлов | READY | finally: producer join, drain, projection finish, journal close/readback, metadata close, terminal report/manifest. Ошибку записи manifest нельзя считать успешным завершением |
| Два запуска на одном порту | PARTIAL | При Windows exclusive COM-open второй ожидаемо завершится ошибкой открытия, но это не протестировано реальным конкурентным открытием в аудите. В приложении нет атомарного резервирования порта/физического USB-устройства |
| Просмотр истории / восстановление | PARTIAL | Можно использовать JSON и schema validation; общего history repository нет. CLI status/verify/recover — заглушки exit 4; отдельный offline `tools.check_stage2_3_record.verify(attempt)` не означает реализацию этих команд |
| Единый session handle / подписки | MISSING | Нет application-объекта start/stop/join/result и очереди событий для GUI. `on_segment` — только callback проверенного закрытого artifact, не общий progress API |

Ссылки для таблицы: [controller](../gnss_logger/recording/controller.py), [contracts](../gnss_logger/recording/contracts.py), [reader](../gnss_logger/recording/reader.py), [storage](../gnss_logger/recording/storage.py), [CLI](../gnss_logger/recording/cli.py), [projection](../gnss_logger/recording/projection.py).

Отдельный [offline verifier](../tools/check_stage2_3_record.py) уже проверяет manifest, hashes/размеры, receipts, ranges и соответствие projection исходному primary. Его можно переиспользовать после небольшой упаковки: сейчас это tools-скрипт с argparse entry point и изменением sys.path при импорте, не стабильный package API. GUI не должен заново реализовывать эти проверки.

Остановка кооперативная, а не мгновенная: serial read ограничен 0.25 с, запись/fsync, readback и verifier могут занять больше. Нельзя обещать «полностью остановлено за 250 мс» или блокировать GUI на join. `run_with_signals(controller, settings)` устанавливает SIGINT и предназначен для CLI/main thread; GUI должен вызывать `run`, не signal wrapper.

## C. Готовность API конвертации

| Возможность | Оценка | Проверенный интерфейс |
| --- | --- | --- |
| Запуск без CLI процесса | READY | `execute(args, sources=None) -> dict` из [conversion/cli.py](../gnss_logger/conversion/cli.py); не печатает. `run(args)` только печатает JSON и возвращает exit_code |
| Независимые настройки операции | PARTIAL | args должен иметь config, output, dry_run; если sources не задан — также input, run_id. allow_obs_without_nav читается через getattr с default False. Нет отдельного ConversionRequest, принимающего Settings напрямую |
| Источники | READY | `select(settings, input_path=None, run_id=None) -> list[Source]`; можно передать готовый list Source в execute, сохранив ownership/fingerprint. Это list, не произвольный одноразовый generator |
| Выходной каталог | READY | Каждая execute создаёт output_root/conversion-UUID; одноимённые stems внутри операции получают segment-N. Предыдущие результаты не перезаписываются |
| Выбор convbin | READY | `load(path=None) -> Settings`: engine.executable, иначе PATH `convbin`, иначе LOCALAPPDATA/GNSS/tools/convbin.exe; probe проверяет файл, hash, CLI options, исключает .cmd/.bat |
| RINEX и OBS без NAV | READY | Неизменяемые Settings содержат version/systems/observations/groups/masks/obs/nav/timeout_s/interval_s/allow_obs_without_nav. TOML + явный флаг; строгий default False |
| Структурированный результат | READY | schema_id/version, operation_id, settings, selected_inputs, segments, summary, boundaries, errors/warnings, report_path, exit_code. По сегменту process, source fingerprint, validation, outputs/published_outputs, result_path, missing_outputs/skip_reason по необходимости |
| Списки пропусков/ошибок | READY | Фильтрация уже готовых segments по status; не требуется пересчитывать в GUI правила валидации или публикации |
| Работа в фоне | PARTIAL | execute/convert блокирующие; запускать целую операцию в worker. SegmentWorker уже асинхронный, но это специальная очередь закрытых recording artifacts, не универсальный job controller |
| Прогресс / сообщения | PARTIAL | warnings/errors/process logs доступны после сегмента/операции. Публичного live callback этапа/сегмента и согласованного snapshot нет |
| Отмена | MISSING | Нет cancel Event/handle. invoke использует subprocess.run(timeout), не предоставляет управляемый Popen. Timeout есть, но это не пользовательская отмена |
| Корректное закрытие GUI | PARTIAL | SegmentWorker.finish() закрывает ingress-флаг и join дренирующей очереди; не отменяет текущий convbin и не имеет deadline. Не вызывать из UI thread |
| Независимость от cwd | PARTIAL | convbin запускается с явным временным cwd, input snapshot и фиксированными локальными именами. Пути TOML разрешаются относительно config; но относительные config/input/output при входе зависят от caller cwd. GUI должен передавать абсолютные Path, не делать os.chdir |

Детали: [engine](../gnss_logger/conversion/engine.py), [config](../gnss_logger/conversion/config.py), [selection](../gnss_logger/conversion/selection.py), [results](../gnss_logger/conversion/results.py), [worker](../gnss_logger/conversion/worker.py).

### Фактические статусы, не предполагаемые GUI

Все статусы конвертера — строки, не общий Enum. `SUCCESS` в [results](../gnss_logger/conversion/results.py) — множество успешных значений, а OBS_ONLY — строковая константа.

| Статус | Семантика / код операции при однородном исходе |
| --- | --- |
| SUCCESS | Проверенные запрошенные outputs опубликованы; 0 |
| SUCCESS_WITH_WARNINGS | То же, warnings или межсегментные boundaries; 0 |
| ALREADY_CONVERTED | Проверенный receipt принят при повторном обращении внутри назначения; входит в SUCCESS, 0 |
| SKIPPED | Валидный undated UBX без RAWX, skip_reason=no_rawx_undated_control_only; convbin не вызывается. Исключён из агрегации. Все skipped → SKIPPED, 0, но никаких RINEX и не «успешная конвертация» |
| FAILED | По умолчанию при исключении/неуспехе; 1. Смесь успешных и ошибочных сегментов тоже остаётся FAILED, не PARTIAL |
| CONFLICT | Чужие/непроверенные занятые outputs/receipt или гонка публикации; все CONFLICT → CONFLICT, 1; смесь с успехом → FAILED |
| INCOMPLETE_NAVIGATION | Ошибка проверки NAV; запрошенная пара не публикуется. В смеси с success/OBS_ONLY → INCOMPLETE_NAVIGATION, 3 |
| OBS_ONLY_INCOMPLETE_NAVIGATION | Только явный opt-in, валидный OBS, convbin=0, NAV именно отсутствует; OBS+receipt опубликованы, missing_outputs=['nav']; 3, не входит в SUCCESS |
| DRY_RUN | Нет публикации; 0 |
| INTERRUPTED / NOT_RUN | KeyboardInterrupt → операция INTERRUPTED, 130; ещё не выполненные сегменты могут остаться NOT_RUN |

Ошибка публикации итогового JSON переводит операцию в FAILED/1 даже при уже опубликованных сегментах. GUI обязан показывать operation-level и segment-level результаты отдельно. В этом конвертере нет общего статуса PARTIAL: нельзя автоматически переносить одноимённый статус старой диагностики/регистрации на конвертацию.

Предупреждения C6I/E6B/E6X и unsupported BeiDou остаются warnings; не повод менять RINEX-профиль. Структурная валидность OBS и успешная публикация не являются научной аттестацией фазовых дуг.

## D. Архитектурные проблемы и приоритет

### BLOCKER — перед безопасным управлением задачами в GUI

1. **Потоковое состояние записи не имеет безопасного контракта.** [RecordController.snapshot / _accounting / _details](../gnss_logger/recording/controller.py) читают несколько изменяемых объектов, а lock защищает только часть счётчиков ingress. Journal, Diagnostics, Projection и их counters меняются вне общего snapshot-lock. Возможны несогласованные accounting, ошибка schema validation, изменение dict при итерации. Это вывод из путей чтения/записи, не воспроизведённая нагрузочная гонка. До run snapshot обращается к None. Просто добавить lock вокруг snapshot недостаточно: writers его сейчас не используют. Нужен owner-thread snapshot, immutable/deep-copy публикация с idle/starting/terminal semantics.
2. **Нет управляемой отмены конвертации.** [invoke](../gnss_logger/conversion/engine.py) прячет subprocess.run; Event не проверяется ни в copy/validation, ни между сегментами [execute](../gnss_logger/conversion/cli.py). Background thread решает отзывчивость UI, но не отмену/выход приложения. До GUI с cancel/закрытием во время работы нужен кооперативный cancel и владение дочерним процессом, его terminate/kill/wait и cleanup. Это не требование менять правила OBS/NAV.
3. **Нет общего арбитра устройства.** [make_reader](../gnss_logger/recording/reader.py) и Stage 1 [main](../gnss_logger/cli.py) открывают порты независимо. Windows защищает открытый COM от второго владельца, но приложение не предотвращает гонку diagnose/record, смену порта или двойной Start. Нужен единый admission guard для операций приложения по COM и USB binding, с освобождением только после финализации; внешние u-center/CLI конфликты всё равно обрабатывать как ошибку открытия.

Эти BLOCKER относятся к заявленному безопасному lifecycle GUI, а не к пригодности текущего CLI для последовательного использования. Прототип просмотрщика завершённых отчётов возможен уже сейчас.

### IMPORTANT — небольшая подготовка API

4. **Orchestration в обработчиках.** [conversion.execute](../gnss_logger/conversion/cli.py) совмещает конфиг/выбор/цикл/агрегацию/публикацию; [recording.preview](../gnss_logger/recording/cli.py) — controller/worker lifecycle; Stage 1 [main](../gnss_logger/cli.py) — открытие/Probe/report/finalization. Выделить application-функции с request/result, оставить CLI адаптером. Не переписывать engine/controller.
5. **Signals и глобальный stdout.** [run_with_signals](../gnss_logger/recording/controller.py) использует signal.signal (main thread); [main](../gnss_logger/cli.py) читает sys.argv по умолчанию, reconfigure stdout/stderr и печатает, argparse.error может вызвать SystemExit. Не вызывать main/preview/run_with_signals из GUI worker. Core run/execute возвращают структуры и не завершают процесс через sys.exit.
6. **Нет общего протокола progress/errors.** [Diagnostics.report](../gnss_logger/diagnostics.py) / [PhaseTracker.report](../gnss_logger/phase.py) не обещают immutable live view; некоторые поля передаются как ссылки. [SegmentWorker](../gnss_logger/conversion/worker.py) имеет изменяемые counts/last_report/last_error, не live API. Добавить bounded event queue с coalescing progress; терминальные results/errors не терять. Callback GUI нельзя выполнять в serial/record owner thread.
7. **Причина stop влияет на полноту.** [PrimaryJournal.close](../gnss_logger/recording/storage.py) признаёт только duration/source_exhausted/stop_requested; UI-facing stop() должен использовать canonical reason и не предлагать произвольные строки. Текущую семантику Ctrl+C/partial сохранить.
8. **История не является API.** [select](../gnss_logger/conversion/selection.py) выбирает последний успешный terminal manifest, а не перечисляет историю всех failed/partial сессий. Нужен read-only loader/index для истории, учитывающий schema_id/version, authoritative manifest, отсутствие/corruption metadata и доступные paths. Не выдавать status/verify/recover за реализованные сервисы.
9. **Разные измерения статуса.** [RecordController.snapshot](../gnss_logger/recording/controller.py) registration, lifecycle State, exit_code, integrity, phase_quality и conversion status независимы. STOPPED_COMPLETE не равен отсутствию decode errors. UI не должен сворачивать их в один зелёный SUCCESS или скрывать опубликованный OBS при exit 3.

### OPTIONAL — можно отложить

10. Общие typed DTO/enum для UI и нормализованные error codes поверх существующих JSON; первоначально достаточно стабильных dict-контрактов и адаптера. Не менять существующие wire schemas только ради GUI.
11. Общий logging sink/ротация технических логов, оценка ETA конвертации, свободный диск в live view (`free_disk_bytes` сейчас None), расширенные фильтры истории. Не путать их с уже имеющимся audit trail.
12. Межпроцессный advisory lock сверх Windows COM exclusive-open — позже, если потребуется координация разных экземпляров приложения; ошибки внешнего владения портом должны оставаться понятными и без такого lock.

### Что не является обнаруженной проблемой

- В core нет прямого парсинга sys.argv, os.chdir и обязательного текстового CLI-протокола; импорт настроек/контрактов не открывает COM.
- Нет глобального singleton приёмника: изменяемое состояние в основном принадлежит экземплярам. Ограничения — process-wide signals/stdout и отсутствие ownership registry, не «глобальный recorder».
- Для разных запусков конвертации уже есть UUID-каталоги; для записи — run/attempt UUID, exclusive files и резервирование имён daily. При гонке публикации действует no-overwrite + rollback только новых файлов. Это стоит сохранить, не заменять GUI-логикой управления файлами.
- Исходный UBX проверяется fingerprint, конвертация работает на временном snapshot. Программный GUI input нельзя подменять незавершённой .part записью; существующие проверки завершённых .ubx остаются обязательны.

## E. Минимальная целевая архитектура

```text
CLI adapter                         Windows GUI adapter
     └──────────── Application API ─────────────┘
                   ├─ device inventory / ownership
                   ├─ RecordingHandle → existing RecordController
                   ├─ ConversionJob → existing converter operation/engine
                   └─ read-only session/result loader
                          ↕ bounded events + immutable snapshots
                   GUI worker / CLI caller (not UI thread)
```

Предлагаемые интерфейсы ниже — план, НЕ существующие функции:

- `start_recording(request) -> RecordingHandle`: владеет одним новым controller, worker и Event; `stop()` вызывает canonical stop_requested, `result()`/join только вне UI, события starting/recording/stopping/finalizing/finished/error. Owner-thread выпускает snapshot после согласованной обработки, GUI читает только опубликованную копию. После stop кнопка Start недоступна до окончания drain/readback/manifest.
- `run_conversion(request, cancel, events) -> report`: перенести существующий execute orchestration в нейтральный модуль, оставить execute(args) совместимым адаптером. Сохранить идентификаторы каталогов, статусы, fingerprints, output receipts, CLI exit codes. Не дублировать агрегацию в GUI.
- Cancellation: проверить Event до snapshot/между блоками и сегментами; для дочернего процесса использовать управляемый handle. Отмена не прерывает атомарный commit посередине; ранее опубликованные сегменты сохраняются, незавершённый work очищается. Новый CANCELLED или отображение в INTERRUPTED сначала согласовать и протестировать как отдельное изменение контракта, не вводить молча.
- `list_devices` / read-only identify: обёртка существующих port_info/Probe/transport; запросы только по явному действию и через ownership. Во время записи не открывать второй порт для MON-VER/VALGET — использовать идентичность/состояние текущего owner.
- `list_sessions/load_result`: read-only чтение имеющихся manifest/report/receipts; не новая система хранения. Отдельно показывать сохранение UBX, целостность, конвертацию, warnings и научные ограничения.

Сохранять dependency injection reader/journal/metadata/clock/projection. Конвертеру при выделении сервиса дать injectable invoker/job runner вместо обязательного global patch subprocess.run. Один общий ApplicationSession для всех видов работ не обязателен: достаточно RecordingHandle и ConversionJob с общим OperationState/events. Связь recording → conversion оставить через on_segment и существующий SegmentWorker, унифицировав lifecycle на уровне адаптера.

Фоновые операции: выделенный worker на запись плюс существующий внутренний producer; последовательная очередь конвертации с ограничением параллелизма; UI получает сообщения через queue/queued signal, не прямой callback изменения виджетов. С PySide6/Qt схема совместима концептуально, фреймворк и зависимости в этом аудите не выбирались и не проверялись. Не переносить serial IO в UI thread и не применять принудительное завершение recording thread.

## F. Тесты и план следующего этапа

### Фактически существующее покрытие

- [test_stage2_record.py](../tests/test_stage2_record.py): прямой controller API через ScriptReader/Clock и injected journal/metadata; deadline, внешний stop Event, single-use, USB exception, identity mismatch/timeout, stale data, overflow, short write, fsync/close/readback/report/manifest errors. Отдельные тесты CLI dispatch и signal wrapper. Параметры физических портов подменены; аппаратный доступ не нужен.
- [test_stage2_contracts.py](../tests/test_stage2_contracts.py): dataclasses, переходы, schemas, CLI-контракты/заглушки.
- [test_stage23_projection.py](../tests/test_stage23_projection.py), [test_stage23_time.py](../tests/test_stage23_time.py), [test_stage23_names.py](../tests/test_stage23_names.py), [test_stage23_crash.py](../tests/test_stage23_crash.py): UTC/undated, ротация, byte coverage, offline verification, ошибки, collision и межпроцессное резервирование, crash durable prefix. Это не тест двух владельцев COM.
- [test_stage31_conversion.py](../tests/test_stage31_conversion.py): большинство end-to-end через main() + JSON stdout с mock subprocess, включая control-only, all-skipped, warnings, OBS-only opt-in/strict, empty/invalid NAV, isolation sequential/parallel, receipt rollback, process timeout/error, интеграцию worker. Также прямые select/load/invoke/stage_report/convert и проверка реального безопасного Python subprocess с пробелами в аргументах.
- [test_stage1.py](../tests/test_stage1.py), [test_stage1_fixes.py](../tests/test_stage1_fixes.py), [test_diagnostics.py](../tests/test_diagnostics.py), [test_saved_logs.py](../tests/test_saved_logs.py): CLI/replay/mock и прямые decoder/Diagnostics/engine тесты; fixtures F9P/X20P синтетические, не доказательство нового аппаратного PASS.

Не найдены тесты согласованного snapshot при одновременном update, snapshot до run, конкурентного start одного controller, application ownership diagnose/record, закрытия GUI во время финализации, отмены настоящего долгоживущего convbin/очереди, пост-cancel публикации, event backpressure, истории повреждённых/незавершённых сессий и GUI thread affinity. Mock KeyboardInterrupt в конвертере не доказывает безопасную отмену subprocess в Windows worker.

### Выполненная безопасная проверка

Python: настроенный существующий `C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe`, версия 3.15.1. Из Git-корня выполнено:

```powershell
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -q
```

Результат: **221 тест, 218 успешных, 2 FAIL, 1 SKIP, 0 errors**, 47.021 с, process exit 1. В том числе конвертер содержит 39 тестов, ошибок этого модуля в полном прогоне нет.

Два FAIL соответствуют уже описанным [известным проблемам диагностики](./known-diagnostics-issues.md):

1. `ProtocolTests.test_all_chunk_boundaries_and_mixed_stream`: discarded_bytes 14 вместо 0.
2. `ProtocolTests.test_sfrbx_length_mismatch_is_not_counted`: ключ с префиксом `02-13:` против ожидания без префикса.

SKIP: `SavedLogTests.test_supplied_log_regression`, локальный пользовательский UBX не распространяется с репозиторием. Причины исторической независимости диагностических сбоев изложены в указанном документе; заново доказательство на старом commit в этом аудите не выполнялось. Неисправности не исправлялись. Тесты создали штатные локальные audit outputs в игнорируемом [reports/sessions](../reports/sessions/); старые результаты не удалены и не перезаписаны.

### Последовательность небольших задач

| № | Цель / компоненты | Зависимости | Критерий завершения |
| --- | --- | --- | --- |
| 1 | Зафиксировать API request/result/status и ошибки; contracts + application adapter, без смены wire formats | Аудит | Прямые Python вызовы дают те же статусы/пути/exit codes, что CLI; strict default и OBS_ONLY/3 сохранены |
| 2 | Выделить orchestration convert и diagnose из CLI; сохранить существующий controller и engine | 1 | CLI тонкий; SegmentWorker не зависит от argparse/CLI; тесты сервиса без stdout parsing, CLI regression зелёный кроме известных baseline |
| 3 | RecordingHandle, owner-thread snapshots и bounded events, canonical stop | 1; может идти параллельно 2 | Idle/start/update/stop/finalization snapshots согласованы, immutable; stop во время read/queue drain сохраняет каждый принятый байт; UI не ждёт join |
| 4 | Ownership устройства и admission guard, единый lifecycle | 3 + сервис диагностики из 2 | Двойной Start и diagnose во время record отвергаются до второго open; разные устройства независимы; cleanup при setup/finalize error освобождает reservation |
| 5 | ConversionJob cancellation и контролируемый subprocess, ограниченная очередь | 2 | Тест долгоживущего дочернего процесса: cancel/timeout, wait/reap, cleanup, нет публикации невалидных outputs; ранее committed outputs не затронуты; cancel между сегментами |
| 6 | Read-only history/result loader и отображение нескольких измерений качества | 1, 2 | Чтение Stage 1/2 и conversion report/receipts, missing/corrupt manifest не превращается в SUCCESS; SKIPPED и OBS_ONLY отображаются отдельно |
| 7 | Offline lifecycle/integration gate | 3–6 | Конкурентные snapshots/stop/start, event overflow, worker shutdown и errors без COM; CLI контракты сохранены; два диагностических baseline ведутся отдельно |
| 8 | Минимальный GUI: выбор устройства/параметры/start-stop/live/results/convert/open folder | 7 | GUI использует только Application API, не parser/stdout, не дублирует IO/валидацию; mock сценарии управляются без оборудования |

Разрешение двух известных диагностических проблем — отдельная задача согласования контракта, не повод переписывать их ожидания в рамках GUI. Перед аппаратной приёмкой GUI требуется явный отдельный план и разрешение пользователя; здесь его нет.

## G. Итоговое заключение

**Ядро пригодно для повторного использования; сначала нужна небольшая подготовка Application API.**

Запись — **PARTIAL для полноценного live GUI**, при этом controller/settings/stop/finalization уже существуют и не требуют новой реализации recorder. Конвертация — **PARTIAL для управляемого GUI job**, хотя программный запуск и структурированные результаты уже READY. Критические недостающие гарантии — согласованные live snapshots, ownership устройства и отмена/завершение фоновых работ.

Можно уже проектировать экраны и read-only просмотр отчётов. Для GUI с реальным Start/Stop/Convert/Close сначала выполнить задачи 1–7 небольшими изменениями, сохранив CLI, рабочий pipeline, аппаратную read-only политику и RINEX-профиль. Переносить алгоритмы в GUI, запускать CLI ради разбора текста или создавать второй recorder/converter не требуется.
