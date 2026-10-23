# Архитектура 0.2.0

Физическое устройство не требуется для работы Replay/Mock, общего декодера и writer.

```text
software u-blox/
├── pyproject.toml
├── README.md
├── stage1_report.md
├── config/station.example.json
├── gnss_logger/
│   ├── __init__.py / __main__.py
│   ├── cli.py                  # serial (alias diagnose), replay, mock, ports
│   ├── engine.py               # общий pipeline и единственный read-loop
│   ├── sources.py              # Chunk, SerialSource, ReplaySource, lossless file_records
│   ├── mock.py                 # MockReceiver, независимые синтетические профили
│   ├── timebase.py             # RAWX→условный UTC и явный UTC-якорь
│   ├── diagnostics.py          # единый обработчик, snapshot, интервалы и качество
│   ├── device/
│   │   ├── ubx.py              # формирование, framing/checksum и декодеры
│   │   ├── transport.py        # COM и проверяемая отправка read-only
│   │   ├── requests.py         # allowlist, Probe, timeout/ACK/NAK/correlation
│   │   └── adapters/
│   │       ├── base.py         # контракт; apply_tec_profile отключён
│   │       ├── f9p.py          # отдельно описанный USB/RAM-поднабор
│   │       └── x20p.py         # поднабор из официального X20 HPG 2.02 JSON
│   └── capture/
│       └── writer.py           # UTCWriter, эксклюзивные сегменты, fsync, SHA-256
├── tests/
│   ├── test_diagnostics.py
│   ├── test_stage1.py
│   ├── test_saved_logs.py
│   └── fixtures/               # MOCK_f9p/x20p/midnight.ubx + synthetic sidecars
├── tools/validate_saved_logs.py # воспроизводимый интеграционный прогон без COM
├── docs/
│   ├── stage1.md               # первоначальное исследование (помечено историческим)
│   ├── protocol.md / adr-001.md / user-context.md / hardware-test.md
│   └── evidence/               # фактические тесты и результаты пользовательского файла
├── data/                       # исходный пользовательский файл, исключён из Git
└── reports/                    # отчёты, копии и сегменты проверок, исключены из Git
```

Поток: Source → engine → точная capture/UTCWriter + Diagnostics/UBXParser → Probe.
Replay использует тот же framing для выделения исходных записей, затем общий обработчик.
Logged polls не являются ответами. Запросы читаются из того же входящего потока, отдельного
потока чтения конфигурации нет. Приёмник не настраивается при старте/остановке локальной записи.

Адаптеры: `detect`, `inspect_configuration`, `validate_tec_profile`, `apply_tec_profile`,
`start_stream`, `stop_stream`. Применение профиля отключено. Валидация пока извлекает только
доступные значения поднабора; это не научная сертификация TEC. Unknown model сохраняет
данные и общую UBX-диагностику, но не получает конфигурационных запросов.

Один сеанс/один владелец COM. Mock не импортирует и не открывает физические порты.
CLI не содержит GUI; будущий UI будет общаться с worker через IPC.

## Следующие компоненты (ещё не реализованы)

```text
capture/worker.py, scheduler.py, recovery.py       # worker process, schedule, reconnect
storage/database.py, manifest.py                  # SQLite и транзакционные очереди
conversion/convbin.py, validation.py               # demo5 b34k audit, RINEX 3.02
upload/auth.py, drive.py, queue.py                 # OAuth, resumable, retry
ui/app.py, models.py                              # после аппаратного CLI-теста
```

Будущие состояния сегмента: recording → closed → converting → validated → uploading → uploaded,
с ошибкой и этапом повтора в БД. Исходный UBX сохраняется. При разрыве/перезапуске — новый
сегмент, без молчаливого склеивания. Текущее сохранение sidecar и JSON ещё не транзакционное.
Второй независимый приёмник подключается после стабильного одиночного сеанса.
