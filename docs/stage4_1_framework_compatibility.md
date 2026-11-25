# Stage 4.1 — совместимость GUI и окружения

Проверено 2026-11-24. GNSS Logger 0.5.0.dev2, HEAD `e71f91e9e46e7d459b23160727d39ca6a870a978`.
Технический проект: [gui_design](stage4_1_gui_design.md). Упаковка: [packaging](stage4_1_packaging_strategy.md).

## 1. Фактическая среда

Проверены sys.version/executable/prefix/base_prefix, struct pointer size, platform и distribution
metadata. Версии из старых README/deployment не принимались за текущие.

| Параметр | Наблюдение |
| --- | --- |
| ОС этого компьютера | Windows-10-10.0.19045-SP0, x64 |
| Python | 3.15.1, build `c37e74c`, Oct 21 2026, MSC v.1951 AMD64 |
| Рабочая venv | `C:/Users/user/AppData/Local/GNSS/.venv` |
| Base runtime | `C:/Users/user/AppData/Local/Python/pythoncore-3.15-64` |
| Рабочие distributions | gnss-logger 0.5.0.dev2; pip 26.3; pyserial 3.5 |
| Минимум проекта | `requires-python = ">=3.9"`, setuptools>=68 для сборки |
| Runtime dependencies | pyserial==3.5; tomli>=2 только Python<3.11 |
| Другие runtimes | старый `py -0p` перечислил 3.8/3.7 и неверно сократил 3.15 до 3.1; ими не пользовались |
| Домашний Windows 11 / Python 3.13.14 | недоступен для фактической проверки; только историческое сообщение пользователя |

Редакторский Python отдельно не отождествлялся с PATH: команды выполнялись полными путями.
Новый Python не устанавливался. В основной venv пакеты не устанавливались/не обновлялись.
Временная среда создана тем же 3.15.1 через `-m venv`, без system-site-packages.

## 2. Официальные данные о пакетах

Сведения получены из PyPI project pages и JSON metadata `/pypi/<package>/json`, затем проверены
установкой там, где указано. Наличие abi3 wheel и Requires-Python — только основание для эксперимента,
не доказательство работоспособности всех функций.

| Пакет | Версия / Requires-Python | Distribution для Windows x64 | Проверка |
| --- | --- | --- | --- |
| PySide6 | 6.12.0 / >=3.10,<3.16 | `pyside6-6.12.0-cp310-abi3-win_amd64.whl` | metadata; полный metapackage не ставился |
| PySide6-Essentials | 6.12.0 / >=3.10,<3.16 | `pyside6_essentials-6.12.0-cp310-abi3-win_amd64.whl` | установлен, Core/Gui/Widgets/QtTest проверены |
| shiboken6 | 6.12.0 / >=3.10,<3.16 | `shiboken6-6.12.0-cp310-abi3-win_amd64.whl` | установлен как dependency |
| Tkinter/ttk | Python stdlib; Tcl/Tk фактически 9.0.4 | не отдельный pip wheel | окно/mainloop/закрытие проверены |
| CustomTkinter | 6.0.0 / >=3.7 | `customtkinter-6.0.0-py3-none-any.whl` | установка, окно/mainloop/закрытие проверены |
| PyInstaller | 6.22.3 / >=3.8,<3.16 | `pyinstaller-6.22.3-py3-none-win_amd64.whl` | установка, onedir build и EXE запуск проверены |
| Nuitka | 4.3.5 / поле Requires-Python не задано | Windows wheel в metadata нет | только обзор; не установлен, не проверен |

Источники: [PySide6](https://pypi.org/project/PySide6/6.12.0/),
[Essentials](https://pypi.org/project/PySide6-Essentials/6.12.0/),
[shiboken6](https://pypi.org/project/shiboken6/6.12.0/),
[CustomTkinter](https://pypi.org/project/customtkinter/6.0.0/),
[PyInstaller](https://pypi.org/project/pyinstaller/6.22.3/),
[Nuitka](https://pypi.org/project/Nuitka/4.3.5/).

В актуальном metadata PySide6 metapackage дополнительно требует Addons, WebEngine и Pdf.
Для Qt Widgets MVP они не нужны: импортный namespace `PySide6` предоставляет уже Essentials.
Проверенный install использовал именно Essentials, а не полный metapackage; размер полного набора
не измерялся. Состав пакетов описан в [Qt package details](https://doc.qt.io/qtforpython-6/package_details.html).

## 3. Сравнение кандидатов

| Критерий | PySide6 / Qt Widgets | Tkinter / ttk | CustomTkinter |
| --- | --- | --- | --- |
| Python 3.15.1 здесь | PASS, Essentials 6.12.0 | PASS, Tcl/Tk 9.0.4 | PASS, 6.0.0 basic smoke |
| Python 3.13.14 | входит в package range, здесь не запускался | зависит от комплектации CPython/Tk, не запускался | metadata допускает, не запускался |
| Windows 10/11 | Qt 6.12 документирует обе x64; локально только Win10 | Python/Tk доступны на Windows; локально Win10 | Windows framework поверх Tk; локально Win10 |
| Sidebar/формы | штатные layout/stack/widgets | Frame+ttk, реализуемо | CTkFrame/buttons, реализуемо |
| История/таблицы | QTableView/model-view, сортировка/делегаты | ttk.Treeview; меньше возможностей model-view | потребуется ttk.Treeview либо своя таблица |
| Фоновая работа | queued signals; проверен QThread→GUI | worker+queue+after; потоковые сценарии здесь не проверены | тот же Tk event loop, требует аналогичного bridge |
| Application API | те же handles/Futures/monitor, без переписывания | совместим по тем же принципам | совместим по тем же принципам |
| UI тесты | QtTest импортирован; полный UI ещё не существует | event_generate/update и mock tests, меньше специализированных средств | Tk тесты и mocks, свои виджеты |
| Поставка | PyInstaller onedir smoke PASS, ~95 МиБ | Tcl/Tk resources надо включать; build не проверен | темы/шрифты + Tk; build не проверен |
| Размер | измерен только минимальный Qt artifact | предположительно меньше, не измерялся | не измерялся; не приписывать ему размер Qt |
| Лицензии | Qt/PySide open-source terms или commercial; по составу модулей | Python PSF и Tcl/Tk licenses | MIT плюс лицензии runtime/dependencies |
| Сопровождение (оценка проекта) | наиболее удобен для четырёх страниц и таблиц | проще dependency stack, больше ручного UI-кода | приятные формы, дополнительная библиотека без выигрыша для таблиц |

Вывод: **выбрать Qt Widgets**, не QML/WebEngine. Tkinter — работоспособный резерв, но после
успешной проверки Qt переключаться на него оснований нет. CustomTkinter не даёт достаточного
преимущества для таблиц/истории. Это инженерный выбор для данного проекта, не общий рейтинг.

## 4. Практические эксперименты

Все установки — только в изолированной временной среде:

```text
C:/Users/user/AppData/Local/Temp/vscode-agent-codex-sandbox-GFsLUl/
  gnss-stage41-07a831f423964f6aa7500b51ef39bdc3/
    venv/
    qt_smoke.py
    stage41-qt-smoke.spec
    build/
    dist/stage41-qt-smoke/
```

Это scratch, не переносимый deliverable и не часть Git. Temp может быть очищен Windows;
проверенные команды и итоги зафиксированы здесь. Бинарники, wheel caches и результаты сборки
в репозиторий не копировались. Вспомогательный qt_smoke.py написан через apply_patch.

Команды эксперимента (после создания venv текущим runtime):

```powershell
# Из отдельного нового временного каталога; основной venv не является целью pip install.
& ./venv/Scripts/python.exe -m pip install --only-binary=:all: PySide6-Essentials==6.12.0 pyinstaller==6.22.3 customtkinter==6.0.0 pyserial==3.5
& ./venv/Scripts/python.exe -m pip check
& ./venv/Scripts/python.exe qt_smoke.py
& ./venv/Scripts/python.exe -m PyInstaller --onedir --console --name stage41-qt-smoke --distpath ./dist --workpath ./build --specpath . ./qt_smoke.py
& ./dist/stage41-qt-smoke/stage41-qt-smoke.exe
```

Установка сначала встретила временные DNS retries files.pythonhosted.org, затем полностью
завершилась exit 0. Две преждевременные попытки import до завершения pip дали ModuleNotFoundError;
после завершения установка и повторные smoke прошли. Это не дефект совместимости PySide/CTk.

Qt smoke: QApplication+QLabel; QObject worker в QThread; Signal(object) с QueuedConnection;
GUI receiver @Slot(object) сверяет thread id и меняет QLabel; worker quit/wait; QTimer heartbeat;
автоматическое закрытие окна через 800 ms и запасной quit через 5 s. Assertions проверяют
разные worker/main threads, GUI delivery, результат 42, ticks>0, закрытие и joined thread.
Это не настоящий recorder и не нагрузочный тест.

| Эксперимент | Итог / наблюдение |
| --- | --- |
| PySide import/Core/Gui/Widgets | PASS, 6.12.0 |
| QtTest QTest/QSignalSpy import | PASS; взаимодействие полного будущего GUI не проверено |
| Python Qt smoke | PASS: main=5888, worker=8284, receiver=5888, ticks=2, exit=0, joined/closed=true |
| Tkinter/ttk | PASS: Tcl=9.0.4, Tk=9.0.4, окно/mainloop/after destroy |
| CustomTkinter | PASS: 6.0.0, CTk+CTkLabel, mainloop/after destroy |
| pip check temporary venv | No broken requirements found |
| Application API, основная venv | 60/60 PASS, 13.097 s |
| Application API, изолированная venv | 60/60 PASS, 8.088 s, запуск из checkout без editable install |
| Полный suite, основная venv, новый прогон | 305 total: 302 PASS, 2 известных FAIL, 1 SKIP, 0 errors, 45.996 s |
| PyInstaller build | PASS, 6.22.3, hooks-contrib 2026.10; ~177 s |
| Native EXE smoke | PASS: main=15984, worker=10164, receiver=15984, ticks=34, exit=0, joined/closed=true |
| Размер onedir | 157 файлов, 99 737 510 bytes (~95.12 MiB); не размер будущего продукта |

При EXE запуске PYTHONHOME/PYTHONPATH очищены, PATH ограничен Windows/System32. В artifact
присутствуют python315.dll, Qt6Widgets.dll, plugins/platforms/qwindows.dll. Но Python остаётся
установленным на машине: это **не clean-machine/no-Python acceptance**. EXE smoke включает
только Qt-окно/worker, не GNSS Logger/convbin/CLI. Подробности дальнейшей проверки — в packaging.

PyInstaller warn-файл содержит условные Unix modules (pwd/grp/fcntl/posix/resource и др.), а также
collections.abc, _frozen_importlib_external, _missing_stdlib_info. Их список не приравнивается к
runtime failure и не объявляется целиком безвредным: минимальный EXE прошёл, полная frozen
программа требует отдельной проверки импортов/ошибок/истории/CLI.

Установленные только в temp дополнительные distributions: shiboken6 6.12.0, altgraph 0.17.5,
darkdetect 0.8.0, packaging 26.4, pefile 2024.8.26, pip 26.2.2, pyinstaller-hooks-contrib 2026.10,
pywin32-ctypes 0.2.3, setuptools 84.2.1. PySerial 3.5 также установлен там для офлайн API tests.
Основная venv сохраняет свой прежний состав.

Полный suite возвращает exit 1 из-за `test_all_chunk_boundaries_and_mixed_stream` (14 != 0)
и `test_sfrbx_length_mismatch_is_not_counted` (ключ `02-13:`); SKIP — локальный saved user log.
Это прежние [известные дефекты](known-diagnostics-issues.md), не новая GUI-регрессия. Тесты не
менялись. После эксперимента для проекта снова выбран исходный interpreter; состав основной
venv повторно проверен и совпадает с таблицей выше.

## 5. ОС и инструменты сборки: подтверждение против предположения

[Qt Windows 6.12](https://doc.qt.io/qt-6/windows.html) перечисляет Windows 10 1809+ x86_64
и Windows 11 x86_64; отмечает последнюю ветку с поддержкой Windows 10. Здесь build 19045 выше
минимума, GUI smoke прошёл. Windows 11 и все patch-релизы Python/Qt не проверены.
Документальная поддержка платформы не гарантирует поддержку ОС её собственным вендором;
политика безопасности/обновлений Windows института — отдельное решение.

[PyInstaller metadata](https://pypi.org/project/pyinstaller/6.22.3/) заявляет Python 3.8–3.15;
build подтверждён для данного 3.15.1. Не экстраполировать на Python 3.16.
[Nuitka manual](https://nuitka.net/user-documentation/user-manual.html) на момент просмотра
перечисляет Python до 3.14 и требует C compiler. Это не доказывает невозможность 3.15 у иных
версий, но достаточного подтверждения для выбора здесь нет. Nuitka отложен, новый toolchain
и другой Python не устанавливались. Его наличие в Qt deployment options само по себе не тест.
[Inno Setup](https://jrsoftware.org/inno-setup) поддерживает Windows 10/11 и установку без
администратора; installer создаётся из frozen artifact и не зависит от Python исходного проекта.
Inno не устанавливался и setup не собирался.

## 6. Рекомендованное окружение

Для Stage 4.2 — отдельная GUI/dev/build venv на **уже установленном CPython 3.15.1 x64**,
PySide6-Essentials 6.12.0, PyInstaller 6.22.3 и зафиксированные зависимости сборки.
Нет необходимости менять Python или рабочую recording venv: тот же runtime и Application API
проверены совместно. Разделение venv нужно для изоляции GUI/build dependencies, а не из-за
несовместимости ядра. Python 3.13 допустим как будущая проверочная среда при наличии runtime,
но сейчас не предлагается как уже проверенный вариант и не устанавливается.

Следующие gates: native DPI 100/125/150%, GUI mock lifecycle/close tests; целый frozen GUI+CLI;
проверка на чистых Win10/Win11 x64 без Python; external convbin и Unicode paths; лицензии,
подпись, installer/update/uninstall. Никакой из этих будущих результатов не объявлен PASS заранее.

## 7. Итог

Qt Widgets на текущем Python/Windows **практически подтверждён в объёме smoke и API tests**.
Standalone минимальное окно тоже работает. Можно начинать Stage 4.2 после согласования;
не требуется новая архитектура ядра. Отдельная venv рекомендуется; отдельный Python не нужен.
Полная Windows 11/no-Python/аппаратная приёмка и проверка окончательной поставки остаются впереди.
