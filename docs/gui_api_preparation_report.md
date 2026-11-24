# Подготовка ядра к безопасному Windows GUI

Дата: 2026-11-23. Версия: 0.5.0.dev2. База: `6c9f2341c506a913b360918a897cd060ec2f2eeb`.
Основание: [архитектурный аудит](./gui_architecture_readiness.md).

## 1. Результат и границы изменений

Реализованы согласованные snapshot записи, управляемая фоновая конвертация с отменой и внутрипроцессное владение устройствами. CLI-команды/параметры, wire-статусы/exit codes, формат исходного UBX, RINEX-профиль, политика публикации OBS/NAV и manifest сохранены. Новой схемы хранения нет.

GUI, редактор TOML, научная обработка и аппаратные тесты не создавались. COM-порты в ходе проверок не открывались; host inventory в тестах подменён, где требуется. Зависимости не устанавливались. Commit/push не выполнялись.

До задачи уже изменены [device/ubx.py](../gnss_logger/device/ubx.py), [diagnostics.py](../gnss_logger/diagnostics.py) и [test_diagnostics.py](../tests/test_diagnostics.py); эти изменения не редактировались. Предыдущий [архитектурный отчёт](./gui_architecture_readiness.md) также уже находился вне индекса. Два известных диагностических сбоя не исправлялись.

### Изменённые и новые файлы этой задачи

| Файл | Изменение/API |
| --- | --- |
| [recording/controller.py](../gnss_logger/recording/controller.py) | Owner-published snapshot, single-use admission lock, lifecycle мониторинга, резервирование устройства до конца финализации |
| [recording/reader.py](../gnss_logger/recording/reader.py) | Использует owned open, передаёт уже проверенные USB-атрибуты; алгоритм чтения прежний |
| [device/ownership.py](../gnss_logger/device/ownership.py), новый | `DeviceRegistry.reserve`, `DeviceLease`, `DeviceBusyError`, общий `devices`; портовые/USB aliases, operation context |
| [device/access.py](../gnss_logger/device/access.py), новый | `open_device`, `usb_identity`, `OwnedConnection`, DI resolver/opener |
| [cli.py](../gnss_logger/cli.py) | Диагностика использует общий guard и owned open; lifetime guard охватывает финализацию отчёта |
| [conversion/job.py](../gnss_logger/conversion/job.py), новый | `ConversionJob.start/cancel/snapshot/result` |
| [conversion/__init__.py](../gnss_logger/conversion/__init__.py) | Экспорт ConversionJob |
| [conversion/cli.py](../gnss_logger/conversion/cli.py) | Совместимый `execute(args, sources=None, *, settings=None, cancel=None, progress=None)` |
| [conversion/engine.py](../gnss_logger/conversion/engine.py) | Кооперативные проверки отмены, cancellable Popen с обязательным wait; прежние проверки/commit |
| [conversion/config.py](../gnss_logger/conversion/config.py) | Валидация прямого создания и dataclasses.replace(Settings); TOML load и ограничения профиля сохранены |
| [conversion/worker.py](../gnss_logger/conversion/worker.py) | `set_enabled`, `cancel_current`; admission/finish под одним lock |
| [test_gui_recording.py](../tests/test_gui_recording.py), новый | 5 тестов snapshot, конкуренции, stop/drain/finalization и изоляции мониторинга |
| [test_gui_conversion.py](../tests/test_gui_conversion.py), новый | 11 тестов job/cancel/settings/auto-worker и дочернего процесса |
| [test_conversion_cancel_integration.py](../tests/test_conversion_cancel_integration.py), новый | Сквозной job с реальным долгоживущим дочерним Python-процессом |
| [test_device_ownership.py](../tests/test_device_ownership.py), новый | 7 тестов ownership/alias/гонок/ошибок и интеграции recording/diagnose |
| [test_stage31_conversion.py](../tests/test_stage31_conversion.py) | Только Popen-compatible адаптер существующей subprocess-фикстуры для worker; прежние assertions сохранены |
| [этот отчёт](./gui_api_preparation_report.md), новый | Решения, примеры, проверки и ограничения |

## 2. Согласованный snapshot записи

### Потоки и решение

В [RecordController](../gnss_logger/recording/controller.py) producer меняет ingress-счётчики/hash под существующим lock, кладёт принятые блоки в ByteQueue и закрывает reader. Вызывающий `run` поток — owner Journal/Diagnostics/Projection, обработки очереди и финализации. Verifier по-прежнему работает штатно через очередь результатов, которые применяет owner.

Публичный `snapshot()` больше не проходит по живым объектам. Owner на завершённых границах обработки формирует диагностический dict, валидирует и сериализует в неизменяемую JSON-строку. Под отдельным коротким lock заменяется только ссылка на опубликованную строку. Потребитель получает ссылку и декодирует вне lock: медленный монитор не удерживает owner и не получает ссылок на внутренние dict/list. Ingress-поля одного snapshot сняты одной короткой операцией под ingress-lock.

Регулярная публикация coalesced до 4 Гц, плюс принудительные lifecycle/terminal публикации. Это последний согласованный снимок, не синхронный запрос к serial IO. Если owner занят fsync/readback/верификацией, снимок остаётся последним опубликованным; UTC в нём позволяет показывать возраст. Опрос GUI чаще 4 Гц не ускоряет публикацию и не запускает дополнительные обходы diagnostics.

`snapshot()` валиден до `run`: wire state остаётся STARTING, а API-only `extensions['gnss_logger.recording']['monitor_phase']` различает:

- `IDLE` — controller ещё не запускался;
- `STARTING` — подготовка/открытие;
- `RUNNING` — producer запущен; точный этап IDENTIFYING/RECORDING находится в обычном `state`;
- `STOPPING` — остановка producer и drain;
- `FINALIZING` — закрытие/верификация/публикация;
- `FINISHED` — run вернул конечный отчёт; успех определяется по registration/integrity/exit_code, не по этому слову;
- `FAILED` — неожиданное исключение вышло из run; `operation_error` объясняет его, финализация не объявляется успешной.

Эти поля добавляются только в опубликованный API-снимок, не меняют сохранённые JSON/manifest. Если построение мониторингового снимка даёт исключение, сохраняются предыдущие согласованные счётчики с `monitor_error`; это не должно прерывать drain/finally. Последующий корректный снимок снимает этот признак.

Одновременный/повторный run одного controller атомарно отклоняется как single-use. Остановка использует прежний Event/request_stop: каждый уже полученный блок сначала учитывается и сохраняется; затем очередь дренируется и проходит прежнюю финализацию. Пользовательская причина должна быть `stop_requested`, а не произвольная строка, меняющая прежнюю complete/partial семантику.

### Программный пример записи — не выполнять в GUI thread

```python
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from gnss_logger.recording.contracts import DeviceBinding, RecordSettings
from gnss_logger.recording.controller import RecordController

# Значения USB берутся из выбранного устройства, не угадываются по имени COM.
binding = DeviceBinding('ZED-X20P', vid, pid, usb_serial=usb_serial)
settings = RecordSettings(
    output=Path(primary_directory).resolve(), initial_port=selected_port,
    binding=binding, seconds=None, baud=115200,
    daily_root=Path(daily_directory).resolve())
controller = RecordController()
executor = ThreadPoolExecutor(max_workers=1)
future = executor.submit(controller.run, settings)

view = controller.snapshot()             # допустимо из UI timer
controller.request_stop('stop_requested')  # запрос, не блокирующее ожидание
if future.done():
    report = future.result()            # обработать и возможное исключение
# executor.shutdown(wait=True) — только после завершения либо вне UI thread.
```

Без USB serial использовать `usb_location=...`, но ровно один selector в DeviceBinding. Для новой записи нужен новый controller. `run_with_signals` остаётся только CLI/main-thread wrapper; GUI его не вызывает и не закрывает serial connection принудительно вместо stop.

## 3. Управляемая конвертация

### API и статусы

[ConversionJob](../gnss_logger/conversion/job.py) — single-use worker:

- `start() -> self`: запускает отдельный поток и немедленно возвращает;
- `cancel()`: выставляет Event, не ждёт завершения;
- `snapshot() -> dict`: detached state, cancel_requested, progress, error;
- `result(wait=False, timeout=None)`: detached report или None, пока не завершено. `wait=True` может блокировать; timeout ожидания не является Cancel. Неожиданное исключение worker повторно выдаётся из result.

Lifecycle: PENDING → RUNNING → SUCCEEDED / FAILED / CANCELLED. Это не новые wire-статусы. SUCCEEDED соответствует exit 0, включая SKIPPED без RINEX; FAILED включает code 3 с неполной навигацией. Поэтому GUI обязательно показывает исходный `report.status`, outputs и warnings: OBS_ONLY не скрывается за общим значком FAILED.

Согласованное отображение отмены: lifecycle `CANCELLED`, отчёт `INTERRUPTED`, exit_code **130** — уже существующий контракт. Нового значения в JSON status enum или новой schema_version не введено. Ошибка публикации отчёта, как прежде, даёт FAILED/1 даже после прерывания; ранее опубликованные outputs остаются в сегментах.

`progress` публикует отделённый отчёт после сегмента; в RUNNING до завершения первого сегмента progress может быть None. Это не процентный прогресс RTKLIB. Job snapshot копирует опубликованные данные вне lock. Логика операции пока остаётся в существующем execute, без масштабного переноса модулей; GUI вызывает Job, не CLI main и не разбирает stdout.

### Дочерний процесс и публикация

При переданном cancel Event [invoke](../gnss_logger/conversion/engine.py) всегда использует Popen(shell=False), проверяет отмену с интервалом ожидания до 50 мс и в finally завершает ещё работающий процесс через kill и **обязательно wait**. Timeout использует тот же cleanup. На Windows это принудительное завершение, но незавершённые выходы находятся только во временном каталоге и не публикуются. Без cancel сохранён прежний subprocess.run путь CLI.

Отмена проверяется до работы с сегментом, после snapshot исходника, между сегментами и непосредственно перед атомарным commit. Сам commit намеренно не прерывается Event посередине. Ранее опубликованные OBS/NAV/receipts сохраняются; незавершённый TemporaryDirectory закрывается после завершения child и удаляется. Могут остаться пустые каталоги операции/segment-N и итоговый JSON прерывания — это не опубликованный RINEX.

Проверки source/executable fingerprint, valid OBS/NAV, symlink запреты и rollback unchanged. Строгий default, SKIPPED control-only, уникальные operation directories, SUCCESS_WITH_WARNINGS и OBS_ONLY_INCOMPLETE_NAVIGATION/3 сохранены. Предупреждения C6I/E6B/E6X и BeiDou не повышались до критических ошибок.

### TOML и программное изменение поддерживаемых настроек

```python
from dataclasses import asdict, replace
from pathlib import Path
from gnss_logger.conversion import ConversionJob
from gnss_logger.conversion.config import load
from gnss_logger.conversion.selection import Source

settings = load(Path(config_path).resolve())  # существующий TOML loader
display_values = asdict(settings)            # данные для будущей формы GUI
settings = replace(settings, timeout_s=1800,
                   allow_obs_without_nav=True)  # явный opt-in, не default

job = ConversionJob(settings=settings,
    sources=[Source(Path(ubx_path).resolve())]).start()
state = job.snapshot()
job.cancel()                         # отдельная команда Cancel
report = job.result()                # None, пока работает; UI не ждёт
```

Прямые Settings и dataclasses.replace теперь валидируются теми же ограничениями профиля: RINEX 3.02, допустимые systems/groups/masks, C+L, bool output flags, positive finite timeout/interval, Path и receiver_model. При изменении systems/groups должны быть переданы согласованные masks, иначе ValueError; валидатор не добавляет новых сигналов. Поля-коллекции Settings — tuples. TOML-парсер по-прежнему переводит списки в tuples и разрешает относительные пути относительно TOML. Для GUI передавать абсолютные paths и не менять cwd процесса.

Settings immutable; новое значение создаётся через replace, не меняет уже запущенную операцию и не пишет TOML на диск. Редактор/сохранение настроек — отдельная задача. `execute(..., settings=...)` сохраняет фактические параметры в report.settings; историческое config_path остаётся указанием пути из args/default, а не доказательством чтения этого файла при программном override.

### Автоматическая конвертация не равна Cancel

```python
from gnss_logger.conversion.worker import SegmentWorker

worker = SegmentWorker(Path(config_path).resolve())
controller.on_segment = worker.submit
worker.set_enabled(False)  # не принимать новые закрытые сегменты
worker.set_enabled(True)   # снова принимать будущие сегменты
worker.cancel_current()    # отдельно отменить текущую операцию
# worker.finish() дренирует ранее принятую очередь и ждёт: не вызывать в UI thread.
```

Отключение auto не отменяет текущую или ранее принятую очередь, не создаёт ручной запуск и не подбирает автоматически сегменты, закрывшиеся при disabled. Ручной запуск — отдельный ConversionJob. Cancel текущего auto-сегмента не отменяет остальные queued segments. Очередь bounded; переполнение по-прежнему учитывается как deferred. finish и submit синхронизированы одним lock: после закрытия новые задания не принимаются.

## 4. Владение приёмниками

[DeviceRegistry](../gnss_logger/device/ownership.py) атомарно резервирует набор aliases:

1. Нормализованный COM (case-insensitive, COM7 и Windows device-prefix alias эквивалентны).
2. USB VID/PID/serial, если известен.
3. USB VID/PID/location, если известен.

Модель и текстовое описание порта не служат физической идентичностью. Контроллер сначала резервирует порт и selector DeviceBinding, затем штатный make_reader проверяет USB и MON-VER прежним способом. При owned open резервирование дополняется всеми известными USB aliases до raw open. Поэтому serial-bound и location-bound обращения к одному наблюдаемому USB-устройству пересекаются, а перенумерация COM не обходит guard при сохранённом serial/location.

Общий `devices` используется записью, CLI диагностикой и новым [open_device](../gnss_logger/device/access.py). Он не ограничивает разные приёмники на разных портах. Резервирование записи удерживается до завершения drain, journal/projection finalization и terminal manifest, а не только до закрытия serial handle. Scope освобождает guard и при исключении; диагностика аналогично удерживает guard до финализации отчёта. Busy выдаётся как DeviceBusyError(OSError) до второго открытия и проходит штатный structured error handling.

Operation lease можно внутренне активировать для единственного owned open; второй attach в том же scope также отвергается. Контекст не даёт другому worker-потоку автоматически заимствовать reservation. OwnedConnection проксирует serial properties/IO, close освобождает только принадлежащее ему резервирование; заимствованное reservation завершает operation scope. Raw [transport](../gnss_logger/device/transport.py) и его read-only allowlist/host serial параметры не менялись.

### Пример для будущего read-only сервиса диагностики

```python
from gnss_logger.device.access import open_device
from gnss_logger.device.ownership import devices, DeviceBusyError

try:
    with devices.reserve(selected_port, binding) as lease:
        with lease.activate():
            with open_device(selected_port, 115200) as connection:
                # Использовать существующие Probe / Diagnostics / run_source.
                # Только разрешённые MON-VER/VALGET, не менять конфигурацию.
                pass
            # scope остаётся занят на время формирования результата/закрытия файлов.
except DeviceBusyError:
    # Устройство занято другой операцией этого приложения; не повторять open.
    pass
```

GUI запускает такой сервис в worker и использует тот же singleton registry, не создаёт отдельный registry на каждую кнопку/операцию. DI `device_registry` у RecordController и resolver/opener у open_device предназначены в том числе для тестов. Ошибка внешнего владения (u-center, другой процесс) остаётся ошибкой serial open, а не обходится этим registry; operation scope освобождается и при ней.

## 5. Проверки

Среда: существующий venv, Python 3.15.1, Windows; новых зависимостей нет. Все источники/портовые интерфейсы тестов синтетические. Реальные дочерние процессы — только экземпляры Python, созданные самими тестами, без COM и GNSS-команд.

| Набор | Результат |
| --- | --- |
| Новые snapshot tests | 5/5 PASS |
| Новые ownership tests | 7/7 PASS |
| Конвертация: прежние 39 + новые 11 | 50/50 PASS |
| Сквозной реальный child внутри ConversionJob | 1/1 PASS |
| Промежуточные регрессии записи Stage 2* | 100/100 PASS |
| Финальный полный набор | **245 total: 242 PASS, 2 известных FAIL, 1 SKIP, 0 errors**; 64.885 с |

Команды из Git-корня (тот же существующий interpreter):

```powershell
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -p test_gui_recording.py -q
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -p test_device_ownership.py -q
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -p '*conversion.py' -q
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -p test_conversion_cancel_integration.py -q
& 'C:/Users/user/AppData/Local/GNSS/.venv/Scripts/python.exe' -m unittest discover -s tests -q
```

24 новых теста прошли. В частности:

- четыре конкурентных snapshot-потребителя во время записи с projection и без неё; schema/accounting invariants, detached вложенные dict, startup и terminal snapshots;
- blocked journal close показывает FINALIZING; Stop не теряет ни одного принятого байта; SHA совпадает;
- медленное декодирование у monitor не удерживает publication lock; monitoring exception не обходит drain/finally;
- восемь одновременных contenders дают ровно один admission; serial/location и COM aliases, record/diagnose conflict до open, разные записи параллельно, guard удержан при финализации и освобождён после ошибок;
- cancel до старта, после snapshot, между сегментами, во время child и перед commit; partial NAV/error не становятся success; disable auto не выставляет Cancel;
- настоящий долгоживущий Python child завершён и reaped при timeout/Cancel; отдельный сквозной job сохраняет первый committed сегмент byte-for-byte, не публикует второй незавершённый OBS и удаляет work directory;
- программные Settings/replace валидируются, Job result/snapshot не предоставляют изменяемые ссылки.

Проверка редактора для изменённых Python-модулей и тестов не показала ошибок; `git diff --check` без замечаний. Выводы сверены с исходниками, реальными вызовами API и результатами выполнения тестов, а не только с именами интерфейсов.

Полный suite ожидаемо возвращает process exit 1 из-за прежних [диагностических проблем](./known-diagnostics-issues.md):

1. `ProtocolTests.test_all_chunk_boundaries_and_mixed_stream`: `discarded_bytes` 14 вместо 0.
2. `ProtocolTests.test_sfrbx_length_mismatch_is_not_counted`: ожидание ключа без `02-13:`.

SKIP: `SavedLogTests.test_supplied_log_regression` — локальный UBX отсутствует в распространяемых fixtures. Эти результаты не являются новым аппаратным PASS. Тесты создавали новые штатные audit outputs в игнорируемом [reports/sessions](../reports/sessions/); предыдущие результаты не удалялись и не перезаписывались.

## 6. Известные ограничения

- Snapshot — согласованный последний опубликованный view, не обещание hard-real-time свежести. Во время медленного storage IO/finalization он может устаревать; GUI показывает возраст и monitor_phase. Построение полного view всё ещё имеет стоимость обхода уже накопленной диагностики, но число мониторов не увеличивает эту стоимость.
- run остаётся блокирующим owner API. GUI обязан запускать его в worker; request_stop неблокирующий, но остановка полностью завершается только после drain/readback/metadata. Нельзя terminate recording thread или закрывать окно с немедленным уничтожением workers.
- Cancel кооперативный: copy/snapshot, fingerprint и RINEX validation имеют границы проверки, не прерываются посреди файловой операции. Во время child polling быстрый, но fsync/kill/wait/cleanup не имеют общего hard timeout. Атомарный commit намеренно заканчивается целиком; поздний Cancel может оставить уже опубликованный сегмент, явно отражённый в отчёте.
- Native convbin запускается без shell. Убивается и ожидается непосредственный child; управление произвольными деревьями wrapper-процессов не добавлено. .cmd/.bat по-прежнему запрещены.
- PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED — API lifecycle, не научная оценка и не замена report.status. В частности SKIPPED/exit 0 не означает наличие файлов, а FAILED/code 3 может содержать валидный опубликованный OBS без NAV.
- Registry действует в пределах одного процесса и общего экземпляра. Прямой вызов низкоуровневого raw transport извне API обходит admission; GUI использует controller/open_device. Внешние программы регулируются OS exclusive open и обычными ошибками.
- При отсутствии USB serial перенос устройства между USB-разъёмами меняет topology identity; если одновременно изменились COM и единственный доступный selector, доказать тождество устройства невозможно. Автоматический reconnect, сканирование/выбор другого порта и изменение конфигурации не добавлялись.
- История сессий, полноценный программный сервис диагностики, общий event bus и редактор TOML по-прежнему не выделены. Их можно добавить адаптерами поверх текущего кода. Старые CLI status/verify/recover остаются прежними заглушками, не выданы за новые API.
- Проверки выполнены в текущем рабочем дереве с известными диагностическими изменениями, не на всех поддерживаемых Python/Windows вариантах. Реальное оборудование в этой задаче не проверялось.

## 7. Следующие задачи и готовность GUI

1. Сделать тонкий GUI-facing coordinator: выбранные USB binding/settings, отдельный recording worker и ConversionJob, обработка результата/исключения и async shutdown. Не переносить serial/decoder/storage/conversion алгоритмы в GUI.
2. Подготовить mock-first интерфейс: Start/Stop, timer snapshot, отдельные Auto / Run conversion / Cancel, раздельное отображение integrity/registration/conversion/scientific warnings и paths.
3. Выделить read-only diagnostics/result-history адаптеры, используя существующие Probe, Diagnostics, manifest/schema/receipts, с тем же ownership. Не запускать второй diagnose на записываемом приёмнике.
4. Протестировать UI thread affinity, закрытие окна при активной записи/Cancel/финализации, ошибки каталога и busy внешнего процесса. GUI-framework пока не выбран, зависимостей для него не добавлено.
5. Два диагностических baseline решать отдельной задачей; аппаратную приёмку выполнять только по новому явному разрешению.

**Можно ли после этих изменений безопасно реализовать GUI с выбором приёмника, Start/Stop, мониторингом записи и запуском/отменой конвертации? Да — используя описанные worker/API и общий registry.**

Три выявленных ограничения устранены на уровне ядра и проверены офлайн. Это готовность к разработке GUI, не готовый GUI и не аппаратная сертификация: UI должен соблюдать асинхронный lifecycle, не блокировать свой поток result/join/finish и не обходить ownership низкоуровневым открытием порта. CLI остаётся самостоятельным интерфейсом с прежними командами и параметрами.
