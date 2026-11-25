# Stage 4.1 — стратегия сборки и распространения

Дата: 2026-11-24. GNSS Logger 0.5.0.dev2. Это проект поставки, не финальный installer.
Основания: [дизайн GUI](stage4_1_gui_design.md), [эксперименты совместимости](stage4_1_framework_compatibility.md),
[существующая инструкция Windows](windows10_deployment.md), [TOML template](../config/conversion.template.toml).

## 1. Выбор инструментария

**Windows x64 + CPython 3.15.1 + PySide6-Essentials 6.12.0 + PyInstaller 6.22.3, onedir.**
Собирать в выделенной venv на Windows; версии wheel/hook dependencies фиксировать с hashes.
Первый релиз — каталог portable в ZIP, затем тот же проверенный payload упаковать Inno Setup.
PyInstaller включает runtime; пользователь не должен устанавливать Python. Общий принцип
описан в [официальной документации](https://pyinstaller.org/en/stable/operating-mode.html).

Onedir предпочтительнее onefile: нет распаковки всей программы при каждом старте, проще анализ
DLL/plugins, замена Qt libraries и раздельное обновление. Это выбор проекта, не утверждение о
невозможности onefile. Окончательный GUI EXE — windowed, CLI EXE — console. Первое минимальное
окно собрано console специально для наблюдения assertions; windowed продукт ещё не проверен.
Одна spec c двумя EXE/shared COLLECT возможна после проверки корректного состава dependencies;
не требовать двух независимых полных копий Qt по умолчанию.

Nuitka не выбран: дополнительный C toolchain без необходимого сейчас выигрыша; просмотренное
[руководство](https://nuitka.net/user-documentation/user-manual.html) не подтверждает используемый
3.15.1 явно. Если PyInstaller выявит конкретное ограничение, провести отдельный сравнительный
эксперимент, не менять runtime основного проекта автоматически.

## 2. Что уже проверено

В изолированном temp созданы QLabel/QThread/queued signal/QTimer smoke, onedir spec/build/dist.
Сборка PyInstaller exit 0; native EXE exit 0; widget обновлён в main thread, worker joined,
окно закрыто. 157 файлов / 99 737 510 bytes (~95.12 MiB), включая python315.dll,
Qt6Widgets.dll и qwindows.dll. Это размер минимального smoke, без GNSS Logger/convbin/данных.
PATH при запуске не содержал Python; PYTHONHOME/PYTHONPATH очищены. Чистая VM без Python не
использовалась — независимость конечной поставки пока не принята. Warnings сборки сохранены
в temp и описаны в [compatibility](stage4_1_framework_compatibility.md); нельзя судить о полноте
сборки только по отсутствию исключения у минимального окна.

## 3. Каталоги приложения и данных

Предлагаемый payload (названия условные, ещё не создавались):

```text
GNSSLogger-<version>-win-x64/
  gnss-logger-gui.exe             # windowed launcher
  gnss-logger.exe                 # console launcher с прежним CLI контрактом
  _internal/                     # Python runtime, package modules, Qt DLL/plugins
  tools/rtklib/convbin.exe        # при разрешённой и проверенной поставке
  resources/                     # GUI resources, не измерения
  config-templates/               # нейтральные примеры, без личных путей/координат
  licenses/                      # тексты/уведомления, source offer/instructions
  build-manifest.json            # версии, commit, component hashes, architecture
```

AppRoot read-only по смыслу: ни UBX, ни RINEX, ни изменяемый TOML туда не пишутся.
Пути runtime resources определять относительно bundle/module, не cwd. Путь к external convbin
должен указывать на реальный файл вне временного workdir конвертации.

```text
%LOCALAPPDATA%/Programs/GNSSLogger/     # per-user installation AppRoot
%LOCALAPPDATA%/GNSS/
  config/gui-preferences.json         # новый GUI-only файл, versioned
  conversion.local.toml               # существующее default имя конвертера
  logs/                               # будущие техлоги GUI, отдельно от audit
  internal/runs/                       # manifest/events/primary journals, existing API
  data/                               # daily UBX по существующему соглашению
  data/rinex/conversion-<UUID>/        # результаты отдельных операций
```

Для больших измерений предложить выбранный пользователем DataRoot, например отдельный NTFS-диск,
и отдельный PrimaryRoot. Existing RecordSettings запрещает совпадающие/вложенные primary и
daily trees. В GUI явно показывать оба пути: нельзя незаметно назначить primary внутрь daily.
Внешний DataRoot предпочтителен для многосуточных записей; локальный default остаётся вне AppRoot.
История знает фактические roots; переезд данных не означает автоматическую миграцию manifest.

Portable означает запуск из распакованного каталога без установки, а не обязательную запись рядом
с EXE. По умолчанию preferences/data тоже вне portable AppRoot. Режим «всё на USB-диске» — будущая
явная опция с проверкой writable paths и объёма; не менять cwd и default_root ядра ради него.

## 4. convbin и существующий TOML

В фактическом локальном TOML настроен внешний binary на рабочем компьютере. Он только прочитан
и хеширован, не запускался/не копировался и не перенастраивался в Stage 4.1:

```text
size:   1 332 736 bytes
SHA256: 6d10e7c09ee918b296ed899cceadd502b9ee308bd7e618a59b24a953b66107c5
```

FileVersion пуст. Имя каталога/demo5 manual не доказывает точный upstream commit/набор патчей.
Перед bundled поставкой установить происхождение именно этого бинарника, source/version/build
и его лицензии; не подменять на случайный upstream convbin и не менять RINEX-профиль.
Сам RTKLIB из другой ветки не автоматически эквивалентен проверенному X20P конвертеру.

Первый безопасный вариант — поддержать существующий пользовательский executable в TOML.
Для готовой поставки разрешено включить проверенный native convbin вместе с required DLL и
licensing materials после отдельного component review. Первый запуск GUI предлагает обнаруженный
bundle tools path и создаёт/сохраняет его только по явному действию; существующий TOML не заменяет.
Программный ConversionSettings(executable=...) тоже поддержан и фиксируется в report.settings.
Loader fallback сейчас ищет PATH и `%LOCALAPPDATA%/GNSS/tools/convbin.exe`, а не новый AppRoot:
GUI должен передать выбранный absolute Path, не рассчитывать на несуществующее автообнаружение.

Для Settings сохраняются прежние engine/input/output/rinex keys и RINEX 3.02. Общий TOML доступен
CLI; defaults GUI находятся отдельно. При отсутствии convbin GUI/история/запись продолжают работать,
конвертация недоступна с понятной причиной. Не включать auto, пока настройки не прошли валидацию.
Не упаковывать `.cmd` настройки приёмников и не запускать их; сохраняется запрет shell wrappers.

## 5. Лицензионная готовность

Это технический checklist распространения, не юридическое заключение. На данном этапе ни
публичная поставка, ни окончательное согласование лицензий не выполнены.

| Компонент | Требуемое действие до распространения |
| --- | --- |
| Собственный GNSS Logger | В корне сейчас нет LICENSE и project license metadata. Выбор лицензии принадлежит владельцу; не придумывать её автоматически |
| Python | Включить тексты Python license и third-party notices runtime |
| PySide6/Qt/shiboken | Выбрать применимые open-source terms либо commercial; зафиксировать конкретный состав модулей и соответствующие notices/source obligations |
| Qt third-party libraries/plugins | Собрать notices именно shipped DLL/plugins; Essentials не означает одну лицензию на все файлы |
| pySerial и прочие runtime dependencies | Сохранить лицензии конкретных distributions, собрать component inventory |
| PyInstaller | GPL с exception для создаваемых bundles, не заменяет лицензии dependencies |
| RTKLIB/convbin fork | Сверить точный source/readme и companion DLL; не считать все версии/форки одинаково лицензированными |
| Inno Setup | Проверить условия выбранной версии/использования перед выпуском; техническая возможность setup не равна лицензированию |

[Qt licenses](https://doc.qt.io/qtforpython-6/licenses.html) и
[Qt LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations) требуют учитывать
лицензии модулей и возможность замены библиотек; подготовить notices, соответствующие исходники
или допустимое предложение их получения, информацию для замены/relink. Не ограничивать такие
права в EULA. Onedir облегчает техническую замену DLL, но сам по себе не доказывает compliance.
Не включать GPL-only add-ons просто потому, что они доступны в полном PySide metapackage.

[PyInstaller license](https://pyinstaller.org/en/stable/license.html) разрешает распространение
собранных приложений при соблюдении лицензий их dependencies. У upstream
[RTKLIB readme](https://github.com/tomojitakasu/RTKLIB/blob/master/readme.txt) BSD 2-clause с
дополнительными условиями, companion components сохраняют собственные лицензии; ранние версии
до 2.4.1 были GPLv3. Для локального неизвестного fork это только ориентир, не подтверждение прав.

CustomTkinter MIT не входит в выбранный runtime. Tk/Tcl тоже не включать без необходимости;
в экспериментальной venv наличие кандидатов не должно определять состав production build.

## 6. Installer, обновление и удаление

Inno Setup упаковывает уже проверенный onedir, не компилирует Python. Цель — per-user установка
без admin, Start Menu shortcuts для GUI и CLI, версия/иконка/действие uninstall.
`PrivilegesRequired=lowest` предусматривается по
[официальному руководству](https://jrsoftware.org/inno-setup/documentation/topic_setup_privilegesrequired.htm).
Windows 10/11 x64 и неадминистративные установки входят в возможности
[Inno Setup](https://jrsoftware.org/inno-setup). Setup здесь не создавался.

Installer не пишет в DataRoot и не включает его в uninstall list; user TOML/preferences/UBX/RINEX/
reports остаются после удаления и обновления. Не использовать wildcard delete вне AppRoot.
Никакого автоматического поиска/удаления старых научных данных. Прежний AppRoot обновлять лишь
после закрытия приложения/финализации операций; показать предупреждение, не убивать recorder.
Runtime/build обновления требуют повторного acceptance. Portable обновлять в новый версионный
каталог, старые данные не перемещать; rollback — сменой AppRoot, не изменением metadata/data.

Подпись EXE/installer и SmartScreen/антивирус acceptance — перед публичным релизом. Certificate
и учётные данные в repo не хранятся. Сборка из чистого checkout: исключить реальные UBX/RINEX,
личный TOML, секреты, reports/sessions, .venv, colab notebooks и экспериментальные caches.
build-manifest содержит только безопасные component/version/hash сведения, без личных путей.

## 7. План проверки поставки

1. На 4.2 собрать mock-only GUI каркас и отдельный CLI launcher; проверить imports/resources,
   console/windowed различие и пользовательские пути из cwd вне AppRoot.
2. Проверить Qt platform plugin, шрифты, иконки, DPI 100/125/150%, русские/длинные пути;
   warn-файл PyInstaller рассмотреть по реальным import paths, а не скрывать целиком.
3. На 4.4 добавить проверенный convbin и отдельные synthetic UBX fixtures в тестовый набор,
   не в научные данные; тестировать OBS/NAV, SKIPPED, OBS_ONLY, timeout/Cancel, published outputs.
4. На чистых Win10 build>=1809 и Win11 x64 без Python/Qt проверить GUI и CLI, stdlib/DLL,
   пропавший/недоступный convbin, права обычного пользователя, PATH без Python, перенос portable.
5. На 4.5 предварительный setup: per-user install/update/uninstall; до/после сравнить hashes
   пользовательских файлов; прерванная установка не должна затронуть DataRoot.
6. На 4.6 системно проверить async close во всех lifecycle, отсутствие живых child после Cancel,
   отсутствие частично опубликованных RINEX и сохранность завершённых сегментов.
7. Только с отдельным разрешением провести аппаратную приёмку F9P/X20P. Наличие минимального
   EXE не является разрешением на COM и не доказывает аппаратную готовность GUI.

## 8. Решение

Стратегия осуществима: минимальный onedir уже проверен. Для Stage 4.2 нового installer/toolchain
не требуется. Release gates остаются: целая frozen программа, clean-machine Win10/11,
происхождение/licensing convbin, лицензия собственного проекта, compliance inventory и безопасные
install/update/uninstall. Основное ядро, алгоритмы записи/конвертации и CLI не изменены.
