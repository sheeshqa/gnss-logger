# Stage 4.5c–d — завершение GUI и подготовка поставки

Дата: 2026-11-26. Состояние: COMPLETED — технические проверки Stage 4.5c–d PASS с указанными ниже ограничениями поставки.

Уточнение: сохранённый frozen aggregate `gnss-stage45cd-frozen-60e22b47571644918e0237004714cf4f/aggregate.json`
показывает requested=1, host_dpr=1.25, QT_SCALE_FACTOR=0.8, actual device_pixel_ratio=1.0.
Текущий runner нормализует множитель по host_dpr; SHA256 acceptance.py остаётся
`bf663c052c5fc506845ae431b2b1292994f3de9fb1cdbb929f947f1784e2104c`.
Хронология уточнена пользователем: сообщение о множителе 1 и actual DPR 1.25
относилось к ранней попытке до исправления; после него выполнены финальные
source и frozen матрицы. Повторная сверка всех шести frozen-строк подтвердила
фактические DPR 1.0/1.25/1.5 при множителях 0.8/1.0/1.2 и host DPR 1.25.
Source snapshot и build manifest итоговой поставки содержат тот же SHA256
acceptance.py; source-verification matched=true. Временный UNACCEPTED снят
после сверки свидетельств, а не игнорирования сбоя. Новая пересборка не нужна.

После обнаруженного timeout silent-uninstall подготовлена исправленная поставка
в новом каталоге `stage45d-final-silent-20261126`; повторная приёмка завершилась PASS.
`stage45d-accepted-20261126` сохранён как предыдущий кандидат и не подменялся.

## Основания и границы

Исходный commit `bcc6c5b79b84870f8cbc6abe5737b21aa754a9c3`.
Baseline: 433 теста, 432 PASS, 0 FAIL, 0 ERROR, 1 известный SKIP;
три последовательных полных прогона успешны. Подробности:
[offline-регрессия](known-diagnostics-issues.md).
Исторические [4.5a–b](stage4_5_ab_report.md) и
[стратегия упаковки](stage4_1_packaging_strategy.md) изучены перед изменениями.

Нумерация по текущему заданию: 4.5c — оформление; 4.5d — сборка;
будущие 4.6c — приёмка установленного приложения, 4.6d — реальные приёмники/convbin.
Эта работа не является разрешением на последние два этапа.

Алгоритмы записи, обработки UBX, конвертации и профиль RINEX не меняются.
Основная `.venv` не используется для установки зависимостей. COM-порты и настоящий
convbin не открываются/не запускаются; научная обработка и Google Drive исключены.
Commit/push не выполняются. Бинарные артефакты находятся вне Git.

## Интерфейс

- [presentation.py](../gnss_logger/gui/presentation.py): общие отступы, размеры
  заголовков, native-контролы, фокус и подсветка выбора, формы с WrapLongRows,
  чередование строк таблиц. Боковое меню и четыре страницы сохранены.
- [main_window.py](../gnss_logger/gui/main_window.py): постоянная нижняя строка
  с портом/подтверждением приёмника, состоянием записи, ручной конвертацией,
  Auto On/Off, длиной очереди и версией. Отсутствующая оценка очереди — «—», не 0.
  Строка остаётся видимой на всех страницах; полный текст доступен в подсказке.
- Оригинальный [SVG-значок](../gnss_logger/gui/resources/gnss-logger.svg),
  версия в заголовке, меню «Справка → О программе» (F1), переходы Ctrl+1…4,
  стрелки в боковом меню и Tab/Shift+Tab. В EXE ICO генерируется из SVG вне Git.
- Полный текст путей сохраняется в полях и подсказках, значения таблиц получают
  подсказки при обновлении. Диагностика не интерпретируется как HTML.
- Минимум окна — 760×540 логических px; начальный размер 1100×750.
  На минимальном размере длинные формы требуют вертикальной и горизонтальной
  прокрутки; длинный заголовок конвертации не помещается целиком в viewport.
  Все контролы доступны прокруткой: это не обещание одновременной видимости формы.
  Журнал событий ограничен прежним числом блоков и показан отдельно.

Native mock-матрица исходников: **6/6 PASS**, 68.817 с; отдельные процессы записи
настроек и повторного запуска на каждом фактическом DPR **1.0 / 1.25 / 1.5**.
Нативный Windows Qt, двенадцать синтетических сессий, сохранение/загрузка TOML,
preferences, история и связь OBS_ONLY, отмена ручной операции и закрытие.
Во всех процессах serial.Serial=0, subprocess.Popen=0; все четыре coordinator
подтвердили shutdown. Сам запуск процессов выполняет внешний тестовый runner.

Масштаб проверялся отдельными Qt-процессами с компенсацией DPR монитора:
множитель QT_SCALE_FACTOR сам по себе не равен фактическому DPI Windows.
Системные настройки Windows не изменялись; физическое переключение DPI,
перенос между разными мониторами и экранные дикторы не проверялись.

Снимки настоящих виджетов, не макеты (для каждого масштаба есть все четыре страницы):

| Страница | 100% | 125% | 150% |
| --- | --- | --- | --- |
| Запись | [снимок](images/stage45cd_source_100_recording_minimum.png) | [снимок](images/stage45cd_source_125_recording_minimum.png) | [снимок](images/stage45cd_source_150_recording_minimum.png) |
| Конвертация | [снимок](images/stage45cd_source_100_conversion_minimum.png) | [снимок](images/stage45cd_source_125_conversion_minimum.png) | [снимок](images/stage45cd_source_150_conversion_minimum.png) |
| История | [снимок](images/stage45cd_source_100_history_minimum.png) | [снимок](images/stage45cd_source_125_history_minimum.png) | [снимок](images/stage45cd_source_150_history_minimum.png) |
| Настройки | [снимок](images/stage45cd_source_100_settings_minimum.png) | [снимок](images/stage45cd_source_125_settings_minimum.png) | [снимок](images/stage45cd_source_150_settings_minimum.png) |

## Проверки

Новые аргументы приёмки требуют `--mock`; проверяются четыре сценария отказа
до создания QApplication: 4/4 PASS (0.015 с).
Итоговый полный регрессионный прогон: **443 теста, 442 PASS, 0 FAIL, 0 ERROR,
1 прежний SKIP**, 173.111 с, exit 0. Относительно baseline добавлено 10 тестов;
новых skip/xfail нет. Прежний SKIP — непоставляемая локальная UBX-фикстура
`SavedLogTests.test_supplied_log_regression`.
Замер истории 1000 сессий: 28.772 с, 5544 heartbeat, max gap 0.546 с.
Это измерение данного окружения, не гарантия скорости на другом компьютере.

Шесть focused presentation tests — PASS (9.736 с), включая динамические подсказки,
состояния очереди, иконку, About и навигацию. Редакторская диагностика новых
GUI-модулей не обнаружила ошибок.

Первый native smoke выявил ошибку фикстуры: одинаковые хеши двенадцати UBX
делали связь конвертации неоднозначной. Исправлены синтетические данные,
не алгоритм HistoryService. Первые попытки DPI проверяли множитель вместо
фактического DPR; runner теперь измеряет базовый DPR и проверяет требуемый.
Эти промежуточные попытки не засчитаны как успешная приёмка.

Raw source matrix:
`C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-eEOMSw/gnss-stage45cd-source-bed0c81107624b82855af5cc7d8b70c6/aggregate.json`.
Harness: [acceptance.py](../gnss_logger/gui/acceptance.py),
[runner](../tests/stage45cd_native_smoke.py). Acceptance-код заморожен с SHA256
`bf663c052c5fc506845ae431b2b1292994f3de9fb1cdbb929f947f1784e2104c`.

Первый полный прогон: 442 теста, 440 PASS, 1 FAIL, 0 ERROR, 1 прежний SKIP,
239.462 с (параллельно шла сборка). FAIL в старом Stage 4.4 тесте
`test_auto_failure_does_not_stop_recording_and_final_queue_is_reported`: после
первоначального FAILED позже публикуется корректный SKIPPED control-only сегмент,
поэтому подпись «Последний результат Auto» больше не обязана содержать FAILED.
Наблюдение пяти отдельных запусков (5 PASS, 70.477 с) подтвердило реальные сигналы
`FAILED → SKIPPED(no_rawx_undated_control_only)`; прежний общий FAIL не скрывается.

Тест теперь проверяет сохранение FAILED=1 в **агрегированной UI-сводке Auto**,
а не неизменность последнего отчёта. Проверки продолжающейся записи, Auto Off,
отсутствия stop_requested, итогового STOPPED_COMPLETE и FAILED=1 в конечном отчёте
сохранены. Отдельная детерминированная регрессия FAILED → SKIPPED подтверждает,
что latest-результат обновляется, а накопленная ошибка не теряется. Ни обработка
очереди, ни политика публикации, ни production-код не исправлялись ради теста.
Изменён только [тест](../tests/test_stage44_integration.py) и добавлена
[presentation-регрессия](../tests/test_stage45_presentation.py).
После исправления соответствующий интеграционный модуль — **7/7 PASS**, 47.603 с;
итоговый полный результат приведён выше.

## Поставка и ограничения

### Изменённые компоненты

Только адаптер GUI, тесты, документация и упаковка:
[app.py](../gnss_logger/gui/app.py),
[main_window.py](../gnss_logger/gui/main_window.py),
[presentation.py](../gnss_logger/gui/presentation.py),
[history.py](../gnss_logger/gui/pages/history.py),
[acceptance.py](../gnss_logger/gui/acceptance.py), SVG-ресурс;
[аргументы](../tests/test_stage45_app_arguments.py),
[оформление](../tests/test_stage45_presentation.py),
[интеграционный тест](../tests/test_stage44_integration.py),
[native runner](../tests/stage45cd_native_smoke.py);
[packaging](../packaging/) и [build tool](../tools/build_stage45d.py),
README, актуализация нумерации плана и текущие отчёты/руководство/снимки.
Прежние recorder, converter, диагностика приёмника, CLI и схемы данных не изменены.

### Артефакты и запуск

Подробный состав, manifest, SHA256 и команды:
[отчёт сборки](stage4_5_build_report.md).
Краткие действия пользователя: [руководство](user_guide_ru.md).

Финальная сборка с проверенными неизменными исходниками:
`C:/Users/user/AppData/Local/GNSS/reports/stage45d-final-silent-20261126`.
`BUILD_COMPLETE.txt` создан только после exit 0 и обеих source-verification.

- [GUI и CLI onedir](../../../reports/stage45d-final-silent-20261126/dist/GNSSLogger/).
- [Portable ZIP](../../../reports/stage45d-final-silent-20261126/GNSSLogger-portable.zip).
- [Inno Setup](../../../reports/stage45d-final-silent-20261126/GNSSLogger-0.5.0.45-setup.exe).
- [SHA256](../../../reports/stage45d-final-silent-20261126/SHA256SUMS.txt).
- [Build manifest](../../../reports/stage45d-final-silent-20261126/dist/GNSSLogger/build-manifest.json).

Для демонстрационного запуска из PowerShell (без оборудования):

```powershell
& 'C:\Users\user\AppData\Local\GNSS\reports\stage45d-final-silent-20261126\dist\GNSSLogger\gnss-logger-gui.exe' --mock
& 'C:\Users\user\AppData\Local\GNSS\reports\stage45d-final-silent-20261126\dist\GNSSLogger\gnss-logger.exe' --help
```

ZIP нужно распаковать целиком; отдельно перенесённый EXE не является portable
поставкой. GUI — PE subsystem 2, CLI — 3; оба содержат значок.
Версия приложения остаётся 0.5.0.dev2; Windows-версия тестовой поставки 0.5.0.45
используется для installer и не меняет версию/контракты ядра.

ZIP дополнительно распакован в новый каталог с пробелами
`C:/Users/user/AppData/Local/GNSS/reports/stage45d portable moved/GNSSLogger`.
Его CLI запущен из другого cwd, PATH содержал только Windows System32,
PYTHONHOME/PYTHONPATH удалены: **PASS, exit 0**. Raw evidence:
`C:/Users/user/AppData/Local/GNSS/reports/stage45d portable moved/cli-validation.json`.
Focused packaging tests: **4/4 PASS**, 0.139 с.

### Найденный и исправленный дефект silent Setup

В первом кандидате custom `MsgBox` требовал OK даже при
`/VERYSILENT /SUPPRESSMSGBOXES`, если приложение занято. Отказ был безопасен,
но unattended проверка не прошла: исходный validator получил timeout 60 с.
Первые два acknowledgement в Inno-логах имеют неизвестный источник;
родительский runner их не посылал. Попытки адресного helper не подтвердили
ни одного нажатия. Явная последующая очистка завершилась exit 0:
exe/регистрация/созданные ярлыки удалены; шесть внешних файлов и неизвестный
файл AppRoot сохранились. Исходный FAIL/timeout не подменяется успешным повтором.

В исправленном [Inno](../packaging/installer.iss) используются две
`SuppressibleMsgBox` с безопасным ответом IDOK; отказ по mutex остаётся False
и не завершает приложение. Это соответствует
[контракту Inno](https://jrsoftware.org/inno-setup/documentation/topic_isxfunc_suppressiblemsgbox.htm).
Отдельная compile-time AppGroup задаёт тестовую группу ярлыков: `/GROUP` не
переопределяет её при скрытой странице выбора. Validator теперь сравнивает
хеши существующих production-ярлыков до и после проверки.

Исправлены только упаковка и её проверки; frozen GUI/CLI и runtime повторно
использованы после сверки исходных freeze-входов и всех хешей/размеров файлов.
Проверены и скопированные байты; provenance сохранён в manifest. Обычная полная
сборка PyInstaller поддержана. Новая поставка имеет новые manifest/ZIP/setup/SHA256;
старые не перезаписывались. Focused packaging tests: **6/6 PASS**, 0.098 с.

### Приёмка исправленной поставки

Исправленный installer проверен **без helpers и нажатий** в новом каталоге:
[validation.json](../../../reports/stage45d-silent-lifecycle-20261126/validation.json).
Отдельные AppId и compile-time группа ярлыков, существующий production-набор
ярлыков сравнивается по SHA256.

| Проверка | Итог |
| --- | --- |
| Установка 0.5.0.45 | exit 0; зарегистрирована 0.5.0.45 |
| Обновление до 0.5.0.46 | exit 0; зарегистрирована 0.5.0.46 |
| Удаление | exit 0; EXE и тестовые ярлыки удалены |
| Занятые GUI/CLI mutex при Setup | оба exit 1, без зависания/подтверждения |
| Занятые GUI/CLI mutex при Uninstall | оба exit 1, без зависания/подтверждения |
| Внешние preferences/TOML/UBX/OBS/runs/archives | все шесть SHA256 неизменны на каждом шаге |
| Неизвестный пользовательский файл внутри AppRoot | сохранён после обновления и удаления |
| Существующие production-ярлыки | не изменились; отдельная тестовая группа удалена |

Inno-логи подтверждают `Defaulting to OK for suppressed message box`; ручное
подтверждение больше не нужно. Исправленный ZIP ещё раз проверен по manifest:
215 файлов, 214 хешей/размеров совпали; manifest не включает собственный хеш.
convbin отсутствует. Новая распакованная копия:
`C:/Users/user/AppData/Local/GNSS/reports/stage45d corrected portable moved/GNSSLogger`;
CLI из foreign cwd без Python в PATH — exit 0.

Проверенные SHA256 итоговых артефактов:

```text
0115b3cec7fcba7c57a0594ffdaa96e761693752013135734a6fceb75309fa55  GNSSLogger-portable.zip
c9839e779cf84b87ebefcd252413805347db87417ce0c20664fe33ea993d2bfe  GNSSLogger-0.5.0.45-setup.exe
```

### Frozen GUI и синтетическая запись

Исправленный portable проверен после переноса в каталог с пробелами, из чужого
рабочего каталога с кириллицей: PATH содержал только Windows/System32,
PYTHONHOME/PYTHONPATH были удалены. **6/6 процессов PASS**, 46.758 с:
запись настроек и повторный запуск при фактическом DPR **1.0 / 1.25 / 1.5**.
Проверены preferences, TOML Save As/reload, история и связь OBS_ONLY, отмена
ручной операции, закрытие всех четырёх coordinators. В каждом процессе
serial.Serial=0 и subprocess.Popen=0; настоящий convbin не запускался.

Отдельный frozen `--mock --smoke-test`: **PASS, exit 0**, шесть синтетических
RAWX-эпох, `STOPPED_COMPLETE`; принято, записано и синхронизировано по 978 байт,
pending/unarchived = 0. Это проверка mock-источника, не аппаратная запись.

Исходные JSON сохранены в
`C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-eEOMSw/gnss-stage45cd-frozen-60e22b47571644918e0237004714cf4f/`:
`aggregate.json` и `recorder-smoke.json`. Четыре frozen-снимка 100% визуально
проверены; ограничения прокрутки на минимальном размере совпадают с source.

### Оставшиеся границы

convbin не включается: происхождение локального executable и право распространения
не подтверждены. Используется прежний внешний executable, явно выбранный через TOML.
Лицензия собственного проекта не выбирается: публичный выпуск не разрешён этой сборкой.
Чистая Windows без Python и Windows 11: NOT TESTED, если отдельной VM нет.
Подпись, SmartScreen, физические DPI/несколько дисплеев и аппаратная приёмка
не подменяются mock-проверками.
Это сборка незакоммиченного рабочего дерева поверх указанного baseline, а не
чистого Git checkout: для её точного состава нужны source SHA256 из manifest,
одного HEAD недостаточно. Сценарий и зависимости воспроизводимы; побитовая
воспроизводимость PE не сертифицирована. После будущей фиксации исходников
релизная сборка должна ссылаться на соответствующий коммит.

## Заключение

Stage 4.5c–d завершены: GUI оформлен, руководство и снимки подготовлены,
GUI/CLI onedir, portable ZIP и установщик созданы и проверены. Полная регрессия:
443 теста, 442 PASS, 0 FAIL, 0 ERROR, 1 прежний SKIP. Source и frozen DPI-матрицы,
синтетический Start/Stop, изолированные установка/обновление/удаление — PASS.
Причины промежуточных сбоев установлены по фактическим трассам и логам;
исправления ограничены тестами и упаковкой, ядро записи/конвертации не менялось.

Поставка пригодна для следующей внутренней приёмки, но не объявляется публичным
релизом до решения лицензионных вопросов и непроверенных сценариев выше.
Stage 4.6c–d не начаты; COM-порты не открывались. Commit/push не выполнялись.
