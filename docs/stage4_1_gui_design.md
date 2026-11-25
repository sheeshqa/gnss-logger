# Stage 4.1 — технический проект GUI

Дата: 2026-11-24. База: `e71f91e9e46e7d459b23160727d39ca6a870a978`, GNSS Logger 0.5.0.dev2.
Статус: проектирование; Stage 4.2 не начат.

## 1. Основания и границы

Проверены [аудит](gui_architecture_readiness.md), [подготовка API](gui_api_preparation_report.md),
[Application API](application_api_readiness_report.md), [известные дефекты](known-diagnostics-issues.md),
[Windows deployment](windows10_deployment.md), [README](../README.md), [pyproject](../pyproject.toml)
и фактический код. Аудит описывает старое состояние: его BLOCKER/IMPORTANT уже закрыты
последующими этапами. Исторические инструкции содержат устаревшие версии и ограничения;
они не являются описанием текущего GUI или доказательством совместимости.

GUI — тонкий русскоязычный Windows-интерфейс записи UBX, конвертации и просмотра результатов.
Один выбранный активный приёмник в MVP; CLI и ядро сохраняют возможность работы с несколькими.
Нет TEC/ROTI, карт, настройки аппаратуры, upload, reconnect, ремонта или удаления данных.
Целевые ОС: Windows 10 x64 и Windows 11 x64; фактическая проверка выполнена только на Windows 10.
Основное окружение, ядро и CLI в Stage 4.1 не изменяются. Три прежние диагностические правки
в рабочем дереве оставлены без вмешательства. COM-порты не перечислялись и не открывались.

## 2. Технологическое решение

Рекомендуется **PySide6 / Qt Widgets 6.12.0**, пакет `PySide6-Essentials==6.12.0`.
Причины: таблицы/model-view, формы, штатная передача результатов между потоками, QtTest,
масштабирование и единое главное окно без WebEngine. Совместимость проверена отдельно в
[матрице окружения](stage4_1_framework_compatibility.md). Поставка — PyInstaller onedir,
затем Inno Setup; см. [стратегию упаковки](stage4_1_packaging_strategy.md).

Версию Qt не обновлять автоматически: Qt 6.12 обозначен последней веткой Windows 10.
GUI оформлять optional extra с собственным ограничением Python 3.10–3.15; общий минимум
CLI `>=3.9` не повышать. Сейчас extra/entry point не добавляются.

## 3. Архитектура и минимальные модули

```text
Qt main thread: MainWindow + pages + Coordinator (состояние представления)
       | команды                     ^ queued signals, detached batches
       v                             |
GUI workers: discovery/diagnose/history + polling worker (250–500 ms)
       |                             |
       +------- gnss_logger.application --------+
                RecordingHandle + ConversionJob + OperationMonitor
                RecordSettings / ConversionSettings / HistoryService
                          |
                прежние controller / converter / DeviceRegistry
                          |
                serial / journal / projection / convbin
CLI ----------------------^  (независимый адаптер того же ядра)
```

Предлагаемая структура, ещё не создана:

```text
gnss_logger/gui/
  __init__.py              # лёгкий импорт; CLI не импортирует Qt
  __main__.py              # python -m gnss_logger.gui
  app.py                   # QApplication, конфиг приложения, entry point
  main_window.py           # боковое меню, QStackedWidget, status bar, closeEvent
  coordinator.py           # выбранное устройство, admission, handles, shutdown
  workers.py               # QObject workers/Futures, polling, queued signals
  preferences.py           # только GUI preferences и адаптер сохранения настроек
  pages/
    recording.py           # форма, команды Start/Stop, готовые метрики
    conversion.py          # форма Settings, запуск/Cancel, таблица сегментов
    history.py             # read-only модель таблицы и подробности
    settings.py            # defaults, TOML, каталоги, информация о версии
  resources/               # локализуемые строки/иконки, без измерений
```

Не создавать собственные recorder, converter, UBX parser, file publisher или session database.
`widgets/` выделять только после появления действительно общих элементов. Отдельный launcher
`gnss-logger-gui = gnss_logger.gui.app:main` и module launcher; существующий `gnss-logger`
сохранить. Не устанавливать обработчики CLI signals из Qt.

Coordinator хранит отдельные ссылки на diagnostic Future, RecordingHandle, manual ConversionJob,
monitor и HistoryService, а не один общий «busy». Одно выбранное устройство блокируется на всю
recording handle lifecycle, включая auto-drain. Ручная конвертация архивов может идти параллельно
записи; в MVP не больше одного ручного job. Auto worker принадлежит RecordingHandle.
При ручной конвертации тех же данных, что уже queued в auto, предупредить о дублировании работы;
изоляция каталогов остаётся ответственностью ядра. Новый глобальный scheduler не требуется.

## 4. Общая компоновка и макеты

Одно resizable окно; sidebar всегда виден, четыре страницы, нижний status bar. Светлая нейтральная
палитра, системные шрифты, моноширинные числовые показатели, подпись и значок вместе с цветом.
Целевой стартовый размер 1100×750 логических px, минимальный 900×600; формы прокручиваются.
Не фиксировать высоты под физические пиксели; проверить 100/125/150%, клавиатуру и focus order.
Длинные пути — selectable text, elide в таблице, полный tooltip и Copy/Open. Русские подписи
через `tr()`, без конкатенации переводимых предложений; английский каталог позже.

### Запись

```text
┌─────────────┬──────────────────────────────────────────────────────┐
│ Запись      │ Устройство [COM… ▼] [Обновить] [Идентифицировать]     │
│ Конвертация │ USB: VID/PID, serial/location  Модель: не подтверждена│
│ История     │ MON-VER: модель / FW / protocol   Доступность: …     │
│ Настройки   ├──────────────────────────────────────────────────────┤
│             │ Служебные runs [путь …]   Суточные UBX [путь …]      │
│             │ Станция [UBX1]  Скорость [115200]                    │
│             │ [●] Длительность [10] [мин/час/сут] [ ] До Stop       │
│             │ [ ] Auto conversion             [Start] [Stop]      │
│             ├──────────────────────────────────────────────────────┤
│             │ Этап …  Время …  Байты …  RAWX …  SFRBX …           │
│             │ Checksum … Decode … Gaps …  Снимок от …              │
│             │ UBX registration … Integrity … Auto …               │
│             │ Файлы / предупреждения / подробности                │
├─────────────┴──────────────────────────────────────────────────────┤
│ Приёмник … | Запись … | Ручная конвертация … | Auto очередь …       │
└────────────────────────────────────────────────────────────────────┘
```

Discovery означает только наличие port metadata. MON-VER подтверждает модель; доступный COM
не означает открытый/проверенный приёмник. При изменении USB/порта прежнюю идентификацию помечать
устаревшей; перед записью ядро повторно проверяет binding. Ни описание USB, ни успешность RAWX
не подменяют идентификацию. Ошибки диагностического качества (exit 2/3) показывать отдельно от
наличия подтверждённой модели. Неподдерживаемая/неизвестная модель не даёт Start.

Параметры формы соответствуют [RecordSettings](../gnss_logger/recording/contracts.py):
`output`, `initial_port`, `binding`, `seconds`, `baud`, `expected_hz`, `station`,
`retention_policy`, `daily_root`. `seconds=None` — до ручной остановки; ввод мин/час/сут переводится
в секунды, без округления числа эпох. `expected_hz` — ожидание диагностики, не команда приёмнику.
`keep_all` — единственная политика хранения, read-only подпись; нет автоудаления и произвольного
периода ротации. Суточная проекция включается `daily_root`, а не GUI-таймером. Два дерева хранения
должны быть раздельны. Queue/sync/identity timeout не являются RecordSettings; в MVP не выводятся.

### Конвертация

```text
│ Источник [UBX / каталог / завершённая сессия] [Выбрать…]             │
│ Выходной корень [путь …]   TOML [путь …] [Загрузить]                 │
│ RINEX [3.02, фикс.]  Системы [G R E C]  Группы [поддерживаемые]      │
│ Интервал […] OBS [✓] NAV [✓] OBS без NAV [ ] Таймаут […] convbin […]│
│ [Run conversion] [Cancel manual]   Auto: On/Off [Cancel current auto]│
│ Этап …   Завершено сегментов N из M / процесс выполняется           │
│ Сегмент | статус | warnings/errors | опубликованные OBS/NAV          │
│ [Открыть каталог] [Отчёт]  Незавершённые NOT_RUN остаются видимыми    │
```

`ConversionRequest.input` — один файл/каталог, `run_id` — выбранный run. Несколько произвольных
файлов поддерживаются `ConversionJob(sources=[Source(...), ...])`, но Source не экспортирован
из facade: использовать готовый [Source](../gnss_logger/conversion/selection.py) как контракт
нижнего слоя, не повторять его selection/валидацию. Для строгого facade-only MVP: один файл,
каталог или run; multi-file picker отложить до совместимого re-export Source. Это небольшая
опциональная API-доработка, а не основание переписывать конвертер. `.part` не предлагать;
файловый suffix сам по себе не доказывает закрытие файла внешним писателем. Для собственных
сессий использовать terminal manifest/готовые artifacts. Ядро проверяет источник и fingerprint.

Из [ConversionSettings](../gnss_logger/conversion/config.py): RINEX строго `3.02`; системы G/R/E/C;
наблюдения обязательно C+L, опционально D/S; группы L1, L2/E5b, L5/E5a, L6; masks только из
существующего профиля с согласованными systems/groups. GUI не добавляет сигналы и не меняет
convbin options. `obs`/`nav`: хотя бы один включён; allow_obs_without_nav допускается только
при запросе OBS+NAV. Пути абсолютные; timeout_s/interval_s конечные положительные.
Форму создавать из loader, изменения валидировать `dataclasses.replace(settings, ...)`.
Новый Settings применяется к новой операции; активные manual/auto jobs не перенастраиваются.

### История

```text
│ История [Обновить]  Фильтр UI: станция / период / текст              │
│ Дата | Приёмник | Длительность | UBX | Integrity | Конвертация       │
│ …                                                                │
│ Подробности: run/attempt, пути, metadata status, warnings/errors     │
│ UBX artifacts | conversion operations | segment receipts            │
│ present / missing; provisional / incomplete / corrupt / ambiguous   │
│ [Открыть файл] [Каталог] [Отчёт] [Конвертировать завершённый UBX]      │
```

Историю загружать в worker через `HistoryService`; простая фильтрация готовых строк — UI.
Receipt не отдельный запуск. Сохранённое `recorded_integrity` и текущее `present` различать;
`independently_verified=False` явно означает отсутствие новой проверки SHA. `diagnostics`
каталога выводить отдельным списком, включая metadata без распознаваемого session_id.
Для неоднозначных копий использовать точный session_key; не связывать по похожему имени.
Open разрешать только существующим файлам, через native path API без shell-команд.

### Настройки

```text
│ Запись: defaults каталогов/baud/станции/длительности/Auto             │
│ Конвертация: TOML / convbin / RINEX root / текущий профиль            │
│ Приложение: RU; poll [250–500 ms]; history roots; версия              │
│ [Применить для следующих операций] [Сохранить…] [Отменить изменения]  │
│ Аппаратная конфигурация не изменяется; upload отсутствует            │
```

Существующий conversion TOML хранит только его engine/input/output/rinex keys. GUI preferences
(геометрия окна, язык, polling, запись defaults, history roots) — отдельный versioned файл
в user config, не новые неизвестные секции TOML. Параметры текущей операции — отдельные immutable
Settings. Будущий TOML writer: сериализовать только разрешённые keys/types в новый файл, проверить
существующим loader, атомарно заменить по явному Save с защитой от внешних изменений и резервной
копией. Не переопределять default CLI config молча. Сохранение комментариев потребует отдельного
решения; в MVP предлагать Save As с предупреждением, без новой зависимости на этом этапе.
Upload можно позже добавить отдельной страницей настроек, сейчас никаких сетевых полномочий.

## 5. Таблица GUI → API

| Элемент/действие | Application API | Результат / отображение |
| --- | --- | --- |
| Обновить устройства | `discover_receivers()` в worker | list metadata; модель ещё не подтверждена |
| Идентифицировать | `DiagnosticRequest(port, seconds=короткий_положительный_интервал)`, `diagnose` в Future | identity/usb_identity/requests/errors; MON-VER, не USB description |
| Диагностика | `DiagnosticRequest(..., inspect=True)` явно, `diagnose` | read-only MON-VER/VALGET; structured quality и ошибки |
| Проверить запись | `DeviceBinding(...)`, `RecordSettings(...)` | ValueError у поля; никакого serial open в форме |
| Start | `RecordingHandle(settings, conversion_settings=...).start()` или `start_recording` | operation_id, неблокирующий старт; повторный Start запрещён coordinator |
| Stop | `handle.stop()` | запрос; ждать terminal result, не только STOPPED |
| Мониторинг | `OperationMonitor(handle, 'recording').refresh()/drain()` | detached events, critical, terminal; UI thread только отображает |
| Auto On/Off | `handle.set_auto_conversion(bool)` | admission будущих сегментов, не Cancel; config задаётся до start |
| Cancel current auto | `handle.cancel_conversion()` | bool текущей операции; queued задания продолжают обрабатываться |
| Загрузить/изменить TOML | `load_conversion_settings(Path)`, `replace` | validated ConversionSettings / ValueError |
| Run conversion | `ConversionJob(ConversionRequest(...), settings=settings).start()` | job snapshot/progress, итоговый report |
| Cancel manual | `job.cancel()` | запрос; terminal INTERRUPTED/130 либо поздно завершившийся commit |
| Результат | `present_result(report, ...)` | независимые статусы и готовые published_files, segments |
| История | `HistoryService(...).list_sessions()/get_session()/list_conversions()` | session_key, операции/receipts, metadata diagnostics |
| Открыть каталог | Qt native local-file URL | только путь из результата/настроек, без пересчёта публикации |

## 6. Метрики без повторной обработки UBX

Корень `handle.snapshot()['recording']`: `state`, `registration`, `accounting.received`,
`last_data_utc`, `last_rawx_utc`, `last_sfrbx_utc`, `last_sync_utc`, `gap_events`, `artifacts`,
`projection` при daily. Из `extensions['gnss_logger.diagnostics']`: `RAWX.epochs`,
`SFRBX.messages`, `decode_errors` (словарь), `checksum_errors`, `timing.gap_events`,
`timing.estimated_missing_epochs`, `phase_quality`. Не декодировать исходные bytes в GUI.

`elapsed` в live diagnostic snapshot не предоставлен отдельным полем: UI показывает помеченное
«время с команды Start» по монотонным часам UI; это не длительность подтверждённых RAWX.
Итоговая длительность истории берётся из готового `duration_s`. Снимок имеет UTC, UI показывает
возраст; возраст не равен времени последнего принятого блока. `free_disk_bytes=None`: не рисовать
фиктивное свободное место; будущая отдельная OS-проверка возможна с явной маркировкой.
`decode_errors` можно суммировать как счётчик представления, не менять классификацию ядра.
Текущий primary `.part` path не гарантирован как готовый artifact: показывать attempt path,
а опубликованные файлы — по artifacts/результату, не угадывать имена.

## 7. Workers, мониторинг и статусы

GUI thread не вызывает блокирующие service/history/result(wait=True)/join/finish. Handle/job уже
создают workers: не оборачивать их в второй recorder/converter. Для discovery/diagnose/history
достаточно ограниченного executor; polling worker по таймеру 250–500 ms вызывает monitor refresh
и drain, передаёт bounded batch через queued Qt signal. Нельзя накапливать неограниченную очередь
Qt signals: один pending batch/ack, transient coalescing; sticky critical/terminal дочитать до конца.
Виджеты меняет только GUI receiver slot. Источники signal/thread affinity проверены smoke-тестом;
полная производительность на суточных metadata проверяется позже. Модель Qt threading:
[QThread](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html).

Каждому запросу discovery/history присваивать generation token, игнорировать устаревшие ответы
после переключения страницы/устройства. История сериализуется собственным lock, не запускать
одновременно много полных сканов. Executor ограничивать; его wait shutdown — вне UI.

Lifecycle не переписывает wire status. `ConversionJob` имеет PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED;
monitor переводит SUCCEEDED в FINISHED. FAILED с exit 3 может содержать опубликованный OBS.
Показывать operation-level и segment-level результаты раздельно:

| Wire status | Отображение |
| --- | --- |
| SUCCESS / SUCCESS_WITH_WARNINGS | Полная запрошенная публикация / публикация с предупреждениями |
| ALREADY_CONVERTED | Уже подтверждённый результат, не повторный запуск convbin |
| SKIPPED | Пропущен с skip_reason; все skipped — файлов нет, не «RINEX готов» |
| FAILED / CONFLICT | Ошибка / конфликт назначения, показать уже published segments |
| INCOMPLETE_NAVIGATION | Неполная навигация; строгая пара не опубликована |
| OBS_ONLY_INCOMPLETE_NAVIGATION | OBS опубликован по opt-in, NAV отсутствует, exit 3 |
| INTERRUPTED / NOT_RUN | Операция прервана / сегмент не запускался |
| DRY_RUN | Проверка плана, публикации нет |

У конвертера нет универсального PARTIAL; не вводить его ради GUI. C6I/E6B/E6X и BeiDou остаются
warnings. Registration/integrity, phase_quality и conversion — независимые панели. Индикаторы
слежения не объявлять доказанными cycle slips или научным PASS.

## 8. Доступность управления

Это состояния представления, не новые JSON enum. Основной источник — raw state + monitor_phase;
terminal результат имеет приоритет над предварительным handle.lifecycle.

| UI state / условие | Start | Stop | Устройство/параметры | Diagnose | Auto / Cancel auto |
| --- | --- | --- | --- | --- | --- |
| IDLE, нет операции | после binding/валидации | нет | да | при выбранном порте | default / нет |
| STARTING | нет | запрос разрешён | заблокированы | нет | если настроен / при active |
| IDENTIFYING (raw state или diagnostic Future) | нет | только для recording handle | заблокированы | нет | только recording |
| RECORDING (RUNNING + raw RECORDING) | нет | да | заблокированы | нет | да / при active |
| STOPPING | нет | отключить повторный клик | заблокированы | нет | Off admission / при active |
| FINALIZING, в том числе auto drain | нет | нет | заблокированы | нет | состояние / при active |
| FINISHED, terminal получен | да при валидных данных | нет | да | да | defaults / нет |
| FAILED, terminal получен | после анализа/валидации | нет | да | да | defaults / нет |

DISCONNECTED/STORAGE_ERROR сначала отображаются как ошибка в ещё активной STOPPING/FINALIZING
операции; не освобождать UI admission до result. Страницы и просмотр истории доступны во всех
состояниях. Manual Run/Cancel управляются своим job независимо от этой таблицы; второй Run запрещён.
Если auto не был сконфигурирован при создании handle, включение On в текущей записи недоступно:
метод set_auto_conversion сам worker не создаёт. Рекомендация: перед Start валидировать Settings,
создать auto-capable handle и при необходимости выставить Off до start.

## 9. Ошибки и асинхронное закрытие

Нет устройства — пустой список и disabled Start; busy внешней программы — structured open error,
предложить закрыть u-center, не повторять open бесконечно. DeviceRegistry общий для всего процесса.
Отключение USB/ошибка диска — показать причину/сохранённый префикс, дождаться финализации; без
автореконнекта, автоудаления и маскировки ошибки. Смена устройства во время handle запрещена.
Нажатие Stop показывает «останавливается», не «завершено».

`closeEvent` при активной работе отклоняет немедленное закрытие и предлагает «Остаться» либо
«Завершить операции и выйти». После подтверждения coordinator ставит closing, запрещает новые
команды, вызывает stop записи. По умолчанию сохраняет Auto до закрытия последнего сегмента;
отключает admission только по отдельному явному выбору с предупреждением, что новые сегменты
не попадут в очередь. Manual job — дать закончить или
отменить явным выбором; Cancel не равен Auto Off. Уже принятая auto-очередь дренируется; отдельная
Cancel current auto не удаляет очередь. В интерфейсе закрытия показывать её остаток и разрешить
отдельный Cancel текущего задания. Не обещать «отменить всё» существующим API.

Диагностика не имеет публичного cancel: использовать короткий срок идентификации (например 5 s),
явный ограниченный срок диагностики, при закрытии дождаться Future. Request timeout не общий
hard deadline; при зависшем системном I/O не обещать мгновенного выхода. Это ограничение MVP,
не скрытая реализация Cancel. Если потребуется немедленная отмена диагностики, отдельная маленькая
API-задача Event в diagnose/run_source и тесты освобождения ownership; в Stage 4.1 не менять.

Polling работает до terminal всех операций; лишь затем остановить timers/polling, завершить
executor/Qt workers вне GUI thread и принять повторное закрытие. Не использовать thread terminate,
os._exit или уничтожение работающего QThread. Не прерывать атомарный commit. После Cancel сохранять
видимость ранее опубликованных outputs; поздно завершившийся commit — реальный результат, не ошибка UI.

## 10. План mock-first тестирования

QtTest (`QTest`, `QSignalSpy`) + unittest; дополнительный pytest-qt возможен позже, не обязателен.
Официальный [QtTest](https://doc.qt.io/qtforpython-6/PySide6/QtTest/QTest.html) предоставляет
инструменты симуляции UI-действий. Headless/offscreen тесты дополнять native Windows smoke.

| Сценарий | Проверяемый критерий |
| --- | --- |
| Нет приёмника / synthetic inventory | старт окна, доступность команд, ноль реальных COM opens |
| Выбор/обновление/MON-VER | stale replies не меняют новый selection; USB не определяет модель |
| Start дважды / diagnose во время record | ровно один handle/open; registry error отображается |
| Запись/Stop/finalization | UI отвечает; все принятые bytes сохранены; controls locked до terminal |
| Slow snapshot / история / overflow | heartbeat UI продолжается; bounded batches, sticky critical/result |
| USB disconnect / disk failure | нет зелёного общего PASS; сохранённые artifacts/причина доступны |
| Manual/Auto/Cancel | Off не отменяет очередь; current Cancel не удаляет queued; два разных controls |
| OBS_ONLY / SKIPPED / conflict | корректные подписи, exit 3 не скрывает OBS, skipped не обещает файлов |
| Cancel после первого сегмента | первый output неизменён, незавершённый второй не публикуется |
| Закрытие при diagnose/record/auto/manual | async wait, никакого destroy активного worker |
| History corrupt/missing/ambiguous | diagnostics видны; сохранённый hash не новая проверка |
| DPI/длинные и русские пути | доступность кнопок, copy/open, таблицы и прокрутка |
| Standalone без Python/convbin отсутствует | запуск UI; actionable conversion error без падения |

В Stage 4.1 заново прошли 60/60 Application API tests в каждой из двух сред.
Также выполнен новый полный прогон: 305 тестов, 302 PASS, 2 известных FAIL, 1 SKIP,
0 errors, 45.996 s; process exit 1 из-за прежних диагностических дефектов. Это тот же baseline,
не полностью зелёный suite. Новые игнорируемые audit outputs созданы штатными тестами,
предыдущие результаты не перезаписывались.
Фактические эксперименты и оставшиеся проверки перечислены в [compatibility](stage4_1_framework_compatibility.md).

## 11. Последующие небольшие этапы

| Этап | Цель / модули | Зависимости | Готовность |
| --- | --- | --- | --- |
| 4.2a | optional extra, launchers, app/main_window/pages, RU строки | Stage 4.1 принят | CLI без Qt работает; 4 страницы, resize/DPI и запуск без устройства |
| 4.2b | coordinator/workers, synthetic providers, polling bridge | 4.2a | нет COM; UI heartbeat, bounded delivery, thread affinity, async close тестируются |
| 4.2c | первый onedir artifact каркаса | 4.2a–b, pinned build env | native запуск на Win10; список DLL/plugins, smoke в чистой VM запланирован |
| 4.3a | discovery/identify/DeviceBinding и RecordSettings формы | 4.2b | модель из MON-VER; mock busy/unknown/stale replies; нет аппаратных изменений |
| 4.3b | Start/Stop/monitor/errors/finalization | 4.3a | synthetic byte accounting, repeated Start/Stop, disconnect/disk/close покрыты |
| 4.4a | Settings/TOML формы, manual job/results | 4.2b, ядро конвертации | профиль неизменен; OBS/NAV/SKIPPED/OBS_ONLY/conflict корректно показаны |
| 4.4b | Auto admission/Cancel, queue и shutdown | 4.3b, 4.4a | Off/Run/Cancel различимы; queue drain, late commit, published outputs сохранены |
| 4.5a | HistoryService table/detail, native open | 4.4a | malformed/provisional/ambiguity и async refresh покрыты; без ремонта/удаления |
| 4.5b | preferences + явный TOML Save As; предварительный Setup | 4.5a, лицензии/packaging review | reload CLI совместим; install/update/uninstall не трогают DataRoot |
| 4.6a | mock/integration/performance acceptance | 4.3–4.5 | documented known failures отдельно; все новые GUI tests проходят |
| 4.6b | чистые Win10/11 VM, portable/setup, no-Python | 4.6a | writable paths, DPI, Unicode, convbin, обе точки входа проверены |
| 4.6c | отдельная аппаратная приёмка F9P/X20P | отдельное разрешение пользователя, 4.6a–b | согласованный тест записи/целостности/RINEX и управляемого выхода |

## 12. Заключение

**Можно приступать к Stage 4.2 после принятия проекта.** Существующего Application API достаточно
для выбранного MVP; переписывание ядра не требуется. Уточнения: multi-file Source не в facade,
нет Cancel diagnostics/всей auto-очереди, нет TOML writer, live elapsed не отдельная метрика,
Windows 11/clean-machine acceptance ещё не выполнены. Эти границы отражены в UI, а не скрыты
новыми wire-статусами. Stage 4.1 не исправляет старые диагностические сбои и не является
аппаратным или научным PASS. Stage 4.2 автоматически не начинается.
