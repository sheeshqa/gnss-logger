# Stage 4.2 — каркас GUI

Дата: 2026-11-24. GNSS Logger 0.5.0.dev2. База HEAD `e71f91e9e46e7d459b23160727d39ca6a870a978`.
Основание: принятый [проект Stage 4.1](stage4_1_gui_design.md),
[совместимость](stage4_1_framework_compatibility.md), [поставка](stage4_1_packaging_strategy.md).

## 1. Объём реализации

Stage 4.2a–c: optional Qt extra/launcher, одно окно с постоянным боковым меню,
четырьмя разделами и status bar, синтетические операции, workers и первый onedir build.
Это каркас, **не рабочая аппаратная панель**. Реальные discovery/diagnose/record/conversion/history
ещё не подключены к виджетам. Их существующие алгоритмы не перенесены в GUI и не заменены mocks.
Stage 4.3–4.6 автоматически не начинались.

Запуск по умолчанию показывает пустой интерфейс без hardware backend. `--mock` явно разрешает
только синтетические устройства MOCK-X20P/MOCK-F9P и фоновую демонстрацию обычного завершения
либо ошибки. Названия MOCK не являются COM-портами или результатами MON-VER.
Шаги демонстрации — UI events, **не RAWX-эпохи**; GNSS bytes, SFRBX, integrity и RINEX не
имитируются как реальные измерения. Никаких UBX/RINEX и научного PASS GUI не создаёт.

## 2. Файлы и границы

| Файлы | Назначение |
| --- | --- |
| [pyproject.toml](../pyproject.toml) | optional extra `gui` с PySide6-Essentials==6.12.0; отдельный gui-scripts entry point |
| [gui/__init__.py](../gnss_logger/gui/__init__.py), [__main__.py](../gnss_logger/gui/__main__.py), [app.py](../gnss_logger/gui/app.py) | lightweight импорт, module/installed launch, dependency guard, `--mock`/`--smoke-test` |
| [main_window.py](../gnss_logger/gui/main_window.py) | главное окно, sidebar/stack/scroll pages, bounded log и close confirmation |
| [coordinator.py](../gnss_logger/gui/coordinator.py) | single-operation admission для demo, selection, tokens, stop latch, асинхронный shutdown |
| [workers.py](../gnss_logger/gui/workers.py) | TaskWorker и MonitorWorker, explicit queued delivery, backpressure |
| [mock.py](../gnss_logger/gui/mock.py) | synthetic inventory и single-use snapshot/result test double, без hardware/файлов |
| [pages/](../gnss_logger/gui/pages/) | recording/conversion/history/settings views и package init |
| [test_stage42_gui.py](../tests/test_stage42_gui.py) | entry/isolation/mock и Qt lifecycle проверки |
| [test_stage42_build.py](../tests/test_stage42_build.py) | no-overwrite, dry-run, command vectors и build failure без PyInstaller запуска |
| [build_gui_stage4_2.py](../tools/build_gui_stage4_2.py), [launcher](../tools/launch_gui_stage4_2.py) | isolated unique onedir GUI/CLI build, без установки зависимостей/удаления |
| [README](../README.md), [инструкция сборки](stage4_2_build.md), этот отчёт | запуск, результаты, ограничения |

Общий `requires-python >=3.9` и CLI entry point сохранены. Qt wheel требует Python >=3.10,<3.16;
GUI проверяет диапазон при запуске, CLI не получает нового требования. Импорт `gnss_logger.gui`
и `gnss_logger.gui.app` не импортирует Qt и не открывает порты. Отсутствующий GUI extra даёт code 2
с сообщением об установке в отдельную среду. Зависимости автоматически не устанавливаются.

Ядро recording/conversion/application/device, профиль RINEX и настройки приёмников не менялись.
До задачи уже были три посторонние правки [device/ubx.py](../gnss_logger/device/ubx.py),
[diagnostics.py](../gnss_logger/diagnostics.py), [test_diagnostics.py](../tests/test_diagnostics.py),
а также три документа Stage 4.1. Они сохранены без вмешательства. Commit/push не выполнялись.

## 3. UI и фоновые операции

Главное окно 1100×750 logical px, минимум 900×600; QScrollArea для каждой страницы, нейтральный
banner с обязательной надписью MOCK. Подписи проходят через `tr()`, база для будущего перевода.
Конвертация/история пока показывают объяснённые disabled controls, а не обещают рабочие команды.
В настройках доступен polling следующей demo-операции 250–2000 ms; на активной задаче он locked.
Настройки не сохраняются, TOML не меняется. Виджеты страниц доступны при активной demo.

TaskWorker получает callable и generation token, возвращает detached value/error. В Stage 4.2
это только синтетический inventory, в будущем — вызов Application API из worker. Задачи имеют
один outcome, ошибки, включая BaseException, не оставляют coordinator навечно busy. Ошибка
запуска потока освобождает admission. При закрытии поздний ответ discovery игнорируется.

DemoOperation — test double, реализующий snapshot/result. **Мониторинг использует настоящий
[OperationMonitor](../gnss_logger/application/monitoring.py)**, не собственный разбор stdout.
RecordingHandle/ConversionJob не дублируются: их интеграция с operational forms — Stage 4.3/4.4.

MonitorWorker вызывает refresh/drain вне GUI thread, выдаёт не более одного неподтверждённого
batch. Следующий batch ждёт acknowledge из GUI slot; underlying monitor сохраняет bounded queue
и sticky critical/terminal. Частота не выше 4 Гц. MainWindow хранит только последний batch и
не больше 200 блоков plain-text лога. Это не бесконечный audit trail; исходные wire statuses
и ядро публикации не изменяются. UI slots проверены на Qt main thread.

Start блокируется до полного освобождения операции, переключение synthetic device при busy
запрещено. Stop — только запрос; queued старый RUNNING не снимает UI stop latch. Окно при busy
предлагает остаться или остановить и дождаться workers. Coordinator не вызывает блокирующие
wait/join в UI и не применяет terminate. Завершение подтверждается terminal result плюс фактическим
окончанием demo-thread/polling task. Callback повторного close queued, поэтому idle-close не рекурсивен.

## 4. Воспроизводимый запуск

Только в отдельной GUI-venv с совместимым Python, не в рабочей recording venv:

```powershell
python -m pip install -e '.[gui]'
python -m gnss_logger.gui --mock
python -m gnss_logger.gui --mock --smoke-test
gnss-logger-gui --mock
```

Команда smoke автоматически выполняет synthetic discovery → demo operation → OperationMonitor
→ terminal result → async shutdown. Встроены heartbeat, thread-delivery проверки и таймер
запроса shutdown через 15 секунд; это не принудительный kill. Если пользовательский/внешний
worker зависнет в системном I/O, hard deadline выхода не гарантируется. В этом этапе callbacks
mock ограничены, оборудования нет.

Исходный GUI работал в уже существующей изолированной CPython 3.15.1 x64 venv Stage 4.1 с
PySide6-Essentials 6.12.0/PyInstaller 6.22.3. Основная recording venv не изменялась.

## 5. Проверки и обнаруженные дефекты

Проверяются: optional import/CLI isolation, отсутствующий Qt, mock без COM, detached results,
4 страницы, selection и double Start, busy controls, heartbeat при slow worker, Stop/close,
отклонённое закрытие, idle-close, late discovery, background error/critical/terminal,
backpressure/slow consumer, polling settings, thread-start failure и BaseException outcome,
queued RUNNING после Stop; отдельно пять безопасных build orchestration tests.

В процессе исправлены собственные дефекты Stage 4.2:

- тест отсутствующего Qt изолирован в subprocess, чтобы не наследовать уже импортированный Qt;
- missing-module guard учитывает submodule name PySide6.QtCore;
- startup exception освобождает TaskWorker/admission;
- Stop сохраняется при позднем progress;
- независимое ревью воспроизвело idle-close recursion: исправлено queued ready_to_close и тестом;
- тест backpressure сначала ждёт полного завершения source перед проверкой terminal batch,
  чтобы не путать корректный промежуточный FINALIZING с terminal result.

### Окончательные результаты

| Проверка | Результат |
| --- | --- |
| Новые Stage 4.2 tests в Qt venv | **24/24 PASS**, 10.250 s |
| Те же tests в исходной venv без Qt | **9 PASS, 15 SKIP**, 2.069 s |
| Source native smoke после idle-close fix | PASS: heartbeat=75, UI delivery=true, exit 0 |
| Source smoke при QT_SCALE_FACTOR=1.5 | PASS: heartbeat=81, UI delivery=true; это не смена Windows DPI |
| Полный повторный suite в Qt venv | **329 total: 326 PASS, 2 известных FAIL, 1 SKIP, 0 errors**, 57.954 s |
| Frozen GUI smoke | PASS: heartbeat=80, UI delivery=true, exit 0 |
| Frozen CLI --help | PASS, exit 0, прежние команды присутствуют |
| CLI --version в исходной venv без Qt | PASS, 0.5.0.dev2 |
| GUI без Qt в исходной venv | корректное сообщение, exit 2 |
| Установленные entry points в изолированной venv | editable install без установки deps: GUI --mock --smoke-test exit 0; CLI --version 0.5.0.dev2 |
| Редакторские проверки Python и git diff --check | без замечаний |

Полный suite возвращает exit 1 из-за двух [известных дефектов диагностики](known-diagnostics-issues.md),
а не объявляется полностью зелёным. Единственный SKIP — отсутствующий локальный saved UBX.
24 новых tests добавлены к прежним 305, существующие assertions не ослаблялись.

Первый полный прогон одновременно с PyInstaller: 319 total, 3 FAIL, 3 ERROR, 1 SKIP,
201.491 s. Помимо двух известных диагностических FAIL, наблюдались timeout result(None) и
WinError32 cleanup в старых conversion/recording tests. Сам по себе этот прогон не доказывает
причину; после окончания сборки требуется последовательный повтор без изменения таймаутов.
Старые tests/ядро ради прохождения не редактировались. Реальных COM и hardware tests не было.

Первый последовательный повтор: 329 total, 325 PASS, 3 FAIL, 1 SKIP, 0 errors, 67.169 s.
Он всё ещё поймал `.convbin-*` после Cancel. Этот же существующий integration test один раз
не прошёл отдельно в основной venv **без GUI imports**, затем прошёл повторно. Выполнена
отдельная копия чистого HEAD через git archive в новом temp-каталоге: импорт engine подтверждён
из копии, тот же test PASS (0.687 s). Наблюдательный повтор текущего теста показал
`SUCCESS_WITH_WARNINGS/[]` и `INTERRUPTED/[]`, без остатка workdir (0.663 s); ещё один обычный
отдельный повтор PASS (0.571 s). Последний полный прогон выше также PASS этого теста.

Вывод ограниченный: наблюдался **нестабильный сбой существующего cancellation/cleanup теста**;
первопричина не установлена, и его нельзя считать исправленным или доказанно вызванным только
нагрузкой. Diff converter/application/самого integration test пуст. Зафиксировать отдельную
проверку перед Stage 4.4: при повторении сохранить segment.errors, PID/завершение дочернего
процесса и состояние файловых handles/workdir. Не менять ожидание отсутствия `.convbin-*` и не
вводить retry/delete вслепую. Исходная и архивная копии не изменяли receiver config.

## 6. Экспериментальная сборка

Два независимых onedir bundle, без попытки окончательного installer/публикации:

```text
C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-GFsLUl/
  gnss-stage4-2/final-frozen-20261124-a1/
    dist/gnss-logger-gui/gnss-logger-gui.exe
    dist/gnss-logger/gnss-logger.exe
    source-hashes-before.json
    source-hashes-after.json
    smoke-results.json
```

GUI: 165 файлов / 102 892 327 bytes (~98.13 MiB), Windows GUI subsystem, qwindows.dll присутствует.
CLI: 24 файла / 24 420 316 bytes (~23.29 MiB), console subsystem, Qt DLL нет. Все 69 hashes
исходников/metadata/build scripts до/после сборки совпали. Нативный запуск выполнен вне bundle
cwd, без Python в PATH и без PYTHONHOME/PYTHONPATH. Поскольку Python установлен на машине,
это **не** проверка чистой Windows без Python. Windows 11 не проверялась.

Подробности versions/plugins/build warnings и команды — в [инструкции сборки](stage4_2_build.md).
В onedir не найдено UBX/OBS/NAV/RNX/TOML; convbin не включён. Рабочее дерево имело прежние
диагностические изменения: это локальная экспериментальная сборка, не релиз из чистого commit.
Каталог temp может быть удалён ОС; переносить нужно **всю onedir-папку**, а не один EXE.

Открыть демонстрацию из проверенного bundle (абсолютный путь этой машины):

```powershell
& 'C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-GFsLUl/gnss-stage4-2/final-frozen-20261124-a1/dist/gnss-logger-gui/gnss-logger-gui.exe' --mock
```

Сборщик отказывается использовать существующий build-id, сохраняет logs и до/после SHA256,
проверяет неизменность source. Старый промежуточный build сохранён отдельно, не перезаписан.
Предупреждения optional/platform modules не объявлены ошибками GUI, но не считаются доказанно
безвредными для всех ещё не проверенных frozen операций ядра.

## 7. Оставшиеся этапы

Stage 4.3: подключить реальные discovery/MON-VER/DiagnosticRequest/RecordSettings/RecordingHandle
через существующий facade и DeviceRegistry; обработать настоящие state/metrics. Stage 4.4:
ConversionSettings/ConversionJob, Auto On/Off отдельно от Cancel, результаты OBS/NAV. Stage 4.5:
HistoryService, preferences/TOML Save As и подготовка installer. Stage 4.6: GUI/packaging acceptance,
Windows 10/11 без Python, DPI/Unicode, затем аппаратная приёмка только по отдельному разрешению.

Нет Cancel диагностики/всей auto-очереди, полноценного TOML writer, persistent preferences,
научной обработки, upload, окончательного установщика/подписи. Это границы будущего этапа,
не скрытые реализованные функции. Каркас не следует использовать для реальной регистрации.
