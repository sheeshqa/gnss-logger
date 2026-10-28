# Stage 2.3 — реализация UTC-ротации

Дата: 2026-10-28. Версия: **0.4.0.dev3**. Основа: cd12c1c (0.4.0.dev2).
План согласован пользователем; выполнены подэтапы 2.3.1–2.3.4.

**Автономная приёмка: PASS. Аппаратная приёмка: NOT TESTED.**
Существующий record без --daily-root сохраняет Stage 2.2/v1. Новая опция включает
суточную проекцию и контракты stage2.3/v2. Commit/push не выполнялись; Stage 2.4 не начат.

## 1. Реализованное поведение

- Primary journal сохраняет исходный поток; суточные файлы читают его synced prefix.
  Единственный reader COM и прежняя bounded ingress queue сохранены.
- --daily-root задаёт пользовательский корень, --output — служебные runs. Вложенные
  друг в друга корни отклоняются; станция проверяется как безопасный Windows-компонент.
- Имена: STATION/YYYY/MM/STATION_YYYYMMDD.ubx, затем _001/_002/... . Открытый файл —
  .ubx.part, одновременно служащий атомарным exclusive reservation. Старые files/parts
  не append/не перезаписываются; final публикуется только no-replace.
- Проекция содержит полные checksum-valid UBX в исходных bytes, включая неизвестные
  class/id с payload до 65535 байт. Noise/checksum-corruption/tail остаются в primary
  и явно классифицируются диапазонами. Содержимое измерений не перекодируется.
- Два RAWX-якоря подтверждают время. SFRBX/прочие сообщения наследуют последний
  подтверждённый якорь в порядке потока до TTL. Неопределённые кадры — STATION/undated/.
- На новом UTC-дне первый RAWX ожидает второй подтверждающий якорь; pending хранится
  как диапазон primary, без накопления исходных bytes в RAM. При остановке неподтверждённый
  диапазон отправляется в undated. Обратной привязки сообщений до первого кандидата нет.
- Таймер, Ctrl+C, source error, drain и финализация сохранены. Partial run не превращается
  в успешный полный run. Внешний hardware status всегда NOT TESTED до ручной приёмки.

Пример структуры новой записи:

```text
GNSS/data/UBX1/2026/10/UBX1_20261028.ubx
GNSS/data/UBX1/2026/10/UBX1_20261028_001.ubx
GNSS/data/UBX1/undated/UBX1_undated_001.ubx
GNSS/internal/runs/<run>/attempts/<attempt>/
  primary/<connection>_<journal>.ubx[.part]
  manifest.start.json, manifest.json, report.json, events.jsonl
  checkpoints/, artifacts/<artifact>.open.json и <artifact>.json
  receipts.jsonl, projection-ranges.jsonl
```

Карта происхождения хранится в receipt/range indexes и artifact records с run/attempt/
connection/journal/artifact IDs. Manifest содержит primary artifact и projection summary
со ссылками на индексы и каталог artifact records. Список всех daily ranges не загружается
в manifest/RAM: это уточнение представления относительно концептуальной таблицы плана.
Каждый файл связан с метаданными независимо от понятного пользовательского имени.

## 2. Политика времени и допуски

Источник UTC: RAWX GPS week + rcvTow - валидный leapS; не календарь Windows и не постоянное
значение 18. Декодер исходных RAWX сохранён. Проверяются длина/версия/finite TOW, week
1024..8191, leapS -64..64, leap validity и clockReset. Диапазоны — консервативные
границы применения Logger (примерно 1999–2137), не заявление о полном диапазоне GNSS.

Период P=1/expected_hz, TTL=max(5 с,3P). Для двух якорей:

- GNSS delta >0 и <=TTL; монотонный arrival delta >=0 и <=TTL;
- отклонение GNSS delta от целого количества периодов <=min(0.1 с,0.1P);
- отличие GNSS delta от arrival delta <=max(2 с,0.2P).

Первые допуски допускают небольшие смещения rcvTow, наблюдавшиеся в логах; более широкий
arrival tolerance учитывает накопление данных/планировщик/чтение USB. Это явно заданная
инженерная политика, а не измеренная гарантия USB latency или абсолютной точности UTC.
Реальные пропуски допускают целое число периодов внутри TTL; крупный скачок, движение назад,
reset, недопустимые флаги или смена leapS сбрасывают доверие и фиксируют новую generation.
После разрыва файлы одной даты не склеиваются.

LeapS-change кадр и неподтверждённая граница не получают предположительную дату; они идут
в undated до нового подтверждения. datetime не представляет 23:59:60. Положительная и
отрицательная смена leapS проверены синтетически; точное восстановление самой leap second
в отдельную календарную метку не реализовано. Новые polling-команды времени не добавлялись.
Ссылки на официальные поля и оговорка о недоступном архивном HPG 1.13 документе — в
[утверждённом плане](stage2_3_implementation_plan.md). Используется наблюдавшийся формат
RAWX v1; полная совместимость всех сообщений/ключей firmware не заявляется.

Фазовые флаги/half-cycle/locktime не меняют маршрутизацию. WARNINGS не означают ошибку
регистрации или доказанные cycle slips; Stage 1 scientific diagnostics не заменены.

## 3. Синхронизация, целостность и ошибки

Receipt index сохраняет offset/sequence/host UTC/monotonic time каждого записанного chunk.
Range index объединяет соседние совместимые source ranges и связывает их с destination
offsets. Индексы append-only, versioned, hashed и fsynced; checkpoint фиксирует их durable
prefix, primary frontier, projection cursor, time candidate/generation, pending offsets,
активный artifact, его written/synced bytes и synced SHA.

Primary sync предшествует проекции. Суточный writer flush/fsync/close, после чего один
read-back worker проверяет SHA/размер. Очередь verifier ограничена четырьмя outstanding
задачами; при перегрузке — явный error/останов, не отбрасывание. Read-back не владеет COM
и не публикует metadata: это делает controller. Одновременно открыт один daily writer.

Ошибка проекции/индекса/публикации останавливает источник, но не отключает drain в исправный
primary. В daily-режиме при primary fsync failure очередь ещё дописывается, если write
остаётся исправным; synced не увеличивается, успешное завершение запрещено. При собственно
primary write failure действует точный accepted-prefix/unarchived учёт. Прежний v1-режим
не меняет свою политику отказа. Добавлены тесты этих различий.

Short daily write оставляет UNVERIFIED, а не verified partial UBX. Partial run с целыми
сохранёнными сообщениями может иметь CLOSED_PARTIAL/.part и корректный SHA; это не full
run. CLOSED_COMPLETE отдельного daily означает завершённый файл, а не полные 24 часа:
partial_day=true и фактические first/last RAWX UTC сохраняются отдельно.

Terminal manifest остаётся последним commit. Report с commit_manifest_required=true не
является самостоятельным доказательством финализации. Ошибки metadata отражаются в
результате; если сам диск не позволяет записать отчёт, остаются доступные intents/parts,
а не обещание, что диагностика всегда может быть записана на отказавший носитель.

## 4. Проверки по подэтапам

| Подэтап | Фактически выполнено | Результат |
|---|---|---|
| 2.3.1 | 4 теста: naming, stale parts, два процесса, unsafe station, v1/v2 contracts | PASS перед 2.3.2 |
| 2.3.2 | 5 первых тестов: splits, checksum/noise, payload 65535, UTC midnight, invalid/reset/leap/TTL | PASS перед интеграцией |
| 2.3.3 | Интеграционные тесты ротации, ranges/SHA, partial stop/restart, отказов write/sync/index/read-back | PASS; первоначальный набор новых тестов 19/19 |
| 2.3.4 | Расширение до 30 новых тестов, force kill, collision, verifier overload, v2 fixtures, полная регрессия | PASS |

Последний полный прогон из основного проекта: **177 tests, 0 failures, 0 errors,
0 skips, 17.344 с**, Python 3.12.14 x64, Windows 11 build 26300.
Это 147 прежних + 30 новых тестов. Существующий serial regression и v1 record tests проходят.
В Git-клоне без личного UBX предусмотрен один документированный skip saved-log regression.

Дополнительно проверен синтаксис 50 Python-файлов с grammar feature_version 3.9–3.15.
Это **не runtime-проверка** Python 3.9/3.10/3.11/3.13/3.14/3.15. Рабочий Python 3.15.1
и Windows 10 будут проверены пользователем по отдельной инструкции.
Обычная editable-установка `pip install -e .` с build isolation выполнена успешно;
`pip check` не нашёл нарушений, CLI --version показал 0.4.0.dev3 и record --help — daily-root.
Попытка без build isolation не подходила домашней venv без setuptools; установка
из инструкции использует обычный pip build isolation, runtime-зависимость pyserial==3.5 сохранена.

Воспроизводимые новые сценарии:

- виртуальная UTC-полночь, GPS week boundary, месяц/год, високосный день, период 30 с;
- invalid/leap/reset/backward/stale, появление времени после undated;
- полный protocol payload, каждый split короткого кадра, corruption/noise/tail;
- два процесса, старые parts, final collision, Ctrl+C/USB -> новый run без изменения старых файлов;
- прекращение до/после полуночи, kill собственного child после durable barrier;
- daily write/short write/fsync, primary fsync, metadata/index/read-back failure;
- ограниченный медленный verifier, fingerprint tampering и synthetic X20P pipeline.

Сценарий force kill проверяет durable prefix и сохранение имён/частичных файлов, **не**
реализует recovery и **не** доказывает сохранение RAM/OS cache при потере питания.

## 5. Replay существующего реального UBX

Прочитан исходный COM4___115200_260305_024016.ubx без изменения. Новая daily-проекция
проверена отдельным offline инструментом в новом временном каталоге:

| Показатель | Фактический результат |
|---|---:|
| Source / primary / derived bytes | 35268374 / 35268374 / 35268374 |
| RAWX / SFRBX | 9804 / 242168 |
| Все целые UBX-сообщения | 252031 |
| Opaque / tail | 0 / 0 |
| Проверенные производные файлы | 1, UBX1_20260305.ubx |
| Первый / последний RAWX UTC | 2026-03-05 02:40:22.003 / 05:23:45.006 |
| SHA-256 source = primary = daily | 4e93d1672d8248bc310335d7636fa60e7b2c4b0231c9ebb114f5797cf86a809e |

Инструмент сравнивает диапазоны исходных bytes, SHA файлов/индексов и framing производных
UBX. Source fingerprint пересчитан до/после. Исторические USB arrival times не известны:
для офлайн TTL используется явно SIMULATED монотонная шкала из RAWX-интервалов.
Отчёт отмечен simulated=true/historical_arrival_unknown=true/NOT TESTED hardware.
Реальный лог проверяет сохранение/проекцию данных, а не задержки/identity текущего COM.
Полночь этим одним реальным файлом не покрыта; она проверена автономно виртуальным временем.

Проверяющий инструмент: tools/check_stage2_3_record.py:

```powershell
python tools\check_stage2_3_record.py verify <ATTEMPT_DIRECTORY>
python tools\check_stage2_3_record.py replay <SOURCE.ubx> --output <NEW_DIRECTORY>
```

verify не меняет артефакты, требует terminal v2 manifest, проверяет владельцев, полное
покрытие receipts/ranges, хеши и побайтовую проекцию. При partial run возвращает отдельный
registration_exit_code. Эти инструменты не меняют зарезервированные status/verify/recover CLI.

## 6. Фактически изменённые файлы

Изменены существующие:

- README.md; pyproject.toml; gnss_logger/__init__.py — 0.4.0.dev3, описание и ссылки.
- gnss_logger/device/ubx.py — настраиваемый framing limit, прежний default сохранён.
- gnss_logger/recording/cli.py, contracts.py — opt-in daily-root и валидация корней.
- gnss_logger/recording/controller.py — интеграция/индексы/финализация и разделение отказов.
- gnss_logger/recording/schema.py — dispatch v1/v2 при неизменной v1-схеме.

Добавлены:

- gnss_logger/capture/names.py, framing.py, daily.py.
- gnss_logger/recording/indexes.py, time_policy.py, projection.py, verification.py, schema_v2.py.
- tests/test_stage23_names.py, test_stage23_time.py, test_stage23_projection.py, test_stage23_crash.py.
- docs/schemas/stage2/{manifest,event,checkpoint,diagnostic,artifact,receipt,range}-v2.json.
- tools/check_stage2_3_record.py.
- docs/stage2_3_hardware_test.md и этот docs/stage2_3_report.md.

Всего: **30 исходных/тестовых/документационных файлов** (29 на первом переносе + отчёт).
Отдельно сохранены локальные ignored evidence: docs/evidence/stage2_3_tests.txt,
stage2_3_tests.json и stage2_3_real_replay.json. Сырые Replay UBX остались вне репозитория.
Сводка контроля защищённых файлов — docs/evidence/stage2_3_checks.json.
Ранее созданные stage2_2_hardware_report.md и stage2_3_implementation_plan.md в этой
реализации не переписывались. Ни один исходный UBX/RINEX не включался в Git.

## 7. Ограничения и дальнейшая приёмка

1. **NOT TESTED аппаратно:** новая версия на рабочем Windows 10/Python 3.15.1/F9P,
   настоящая UTC-полночь, длительная нагрузка, физические USB/диск/питание. X20P только synthetic.
2. Reconnect — 2.4, full recovery — 2.6. Старые part не переиспользуются, автоматического
   восстановления/очистки/удаления нет. Ручной новый run всегда имеет новые IDs/суффикс.
3. Sync/fsync плюс checkpoint не гарантируют отсутствие потери несинхронизированной RAM
   при kill/потере питания. Семантика controller остаётся «все returned bytes учтены».
4. Отсутствие пропусков и длительная производительность нового I/O не сертифицированы
   короткими тестами; bounded verifier явно останавливает run при невозможности успевать.
5. Дополнительное хранение primary+daily+indexes требует места; auto deletion и advanced
   disk-space policy не добавлялись. Следить за свободным местом вручную.
6. Absolute artifact paths остаются свойством существующего формата. При переносе bundle
   между компьютерами нужен внешний mapping корней; originals metadata не переписывать.
7. Нет RINEX/TEC/ROTI/Drive/GUI. Фазовая пригодность и аппаратная целостность Stage 2.2
   по непереданным файлам не подменяются автономным PASS нового этапа.

Подробные точные команды и таблица аппаратных критериев:
[stage2_3_hardware_test.md](stage2_3_hardware_test.md).
Ожидается проверка пользователем и отдельное решение об аппаратной приёмке Stage 2.3.
