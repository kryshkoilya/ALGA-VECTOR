# ALGA VECTOR 0.7.0

ALGA VECTOR — русскоязычный offline-first Windows foundation гражданской
мультисенсорной системы раннего предупреждения. В одном локальном приложении
объединены пассивный RF-мониторинг, безопасный акустический analysis core,
гражданский ADS-B-контекст, внешний измеренный bearing и объяснимая временная
fusion-логика.

> Разработал: Буйвол и Задира

![SIMPLE MODE ALGA VECTOR 0.7.0](docs/screenshots/simple-mode-070.png)

## GitHub-навигация

- **Скачать:** [GitHub Releases](../../releases)
- **Все версии и SHA-256:** [`VERSIONS.md`](VERSIONS.md)
- **Полное устройство проекта:** [`docs/GITHUB_PROJECT_OVERVIEW_RU.md`](docs/GITHUB_PROJECT_OVERVIEW_RU.md)
- **Архитектура SIMPLE/EXPERT:** [`docs/SIMPLE_EXPERT_ARCHITECTURE_RU.md`](docs/SIMPLE_EXPERT_ARCHITECTURE_RU.md)
- **Журнал разработки с OpenAI Codex:** [`docs/DEVELOPMENT_DIALOG_RU.md`](docs/DEVELOPMENT_DIALOG_RU.md)
- **Быстрый старт:** [`docs/QUICK_START_RU.txt`](docs/QUICK_START_RU.txt)
- **Сторонние компоненты:** [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
- **Безопасность:** [`SECURITY.md`](SECURITY.md)
- **Участие в разработке:** [`CONTRIBUTING.md`](CONTRIBUTING.md)

> Для новой установки используйте только последний release и сверяйте
> SHA-256. Windows EXE пока не имеют Authenticode-подписи. Исторические
> версии публикуются как legacy binaries без восстановленного source snapshot.
>
> Отдельная лицензия на исходный код ALGA VECTOR владельцами пока не выбрана.
> Публикация репозитория не отменяет лицензии сторонних компонентов и сама по
> себе не выдаёт разрешение на повторное распространение кода продукта.

Версия 0.7.0 даёт запускаемый production-foundation с одним backend и двумя
режимами интерфейса:

```text
PySide6 shell → SIMPLE MODE / EXPERT MODE → structured logs
       ↓
RF + acoustic + civilian ADS-B context + optional validated bearing
       ↓
quality gates → normalized event bus → human-readable interpretation
```

Foundation можно запускать локально в Live, Safe и явно маркированном Demo.
Demo использует детерминированные fake-источники и никогда не выдаёт их за
реальные измерения. Structured JSONL-логи, диагностика, onboarding и Windows
PyInstaller/Inno Setup skeleton входят в проект.

Акустический модуль 0.7.0 содержит PCM feature/detection core и fake-source для
проверяемого Demo. Bundled live-захват с микрофона и полевая модель конкретного
физического объекта пока не поставляются. ADS-B-модуль читает локальный
`dump1090 aircraft.json` как гражданский контекст; отсутствие борта в ADS-B не
является признаком угрозы. Fusion core объединяет только валидные, свежие
наблюдения и может честно воздержаться от решения.

## Граница достоверности

ALGA VECTOR:

- принимает данные пассивных сенсоров, но не передаёт и не подавляет радиосигнал;
- описывает форму, частоту, полосу, относительный уровень, длительность,
  повторяемость и качество наблюдения;
- не идентифицирует передатчик или физический объект по одному спектру;
- не определяет координаты, дальность, траекторию или точную геолокацию;
- не вычисляет направление по уровню одного tinySA, RTL-SDR или HackRF;
- не определяет государство, принадлежность, намерение, «свой/чужой», IFF или
  военный тип объекта;
- не содержит conflict-specific базы частот или национальных сигнатур;
- не выдаёт эвристический score за калиброванную вероятность.

`voice_like`, `packet_like` и другие классы означают только совместимость
измеренной RF-формы с общим шаблоном. Они не доказывают назначение сигнала,
протокол или тип физического источника. Акустические классы также являются
общими описаниями признаков, а не идентификацией объекта.

## Рабочие разделы

- **Простая обстановка** — крупный human-readable статус, объяснение, валидный
  сектор, сила подтверждения, следующий шаг и важные события;
- **Обзор** — состояние сенсоров, fusion-итог, объяснение и следующий шаг;
- **Устройства** — безопасное обнаружение, явное добавление и диагностика;
- **Спектр** — live-график, waterfall, capability-driven tuning и
  последовательный автообзор общих диапазонов;
- **События** — lifecycle RF-эпизодов, evidence, alternatives и ограничения;
- **Направление** — валидный bearing либо честное пустое состояние;
- **Карта** — экспертная картографическая страница; она не строит позицию
  источника по одному азимуту или уровню сигнала;
- **Диагностика** — incidents, health, журнал и локальный support bundle;
- **Настройки** — профиль, хранилище, приёмники и параметры спектра.

Интерфейс построен на PySide6, использует русский язык по умолчанию и одинаково
явно маркирует Live, Safe, Demo, stale и unavailable.

## Поддерживаемые приёмники

| Приёмник | Явное подключение | Discovery | Измерение |
|---|---|---|---|
| HackRF One | `HACKRF:<hex-серийный номер>` | официальный `hackrf_info` с timeout | signed 8-bit IQ через receive-only `hackrf_transfer -r -`, spectrum в dBFS |
| PortaPack + HackRF | как HackRF One | только после ручного перехода в HackRF USB mode | тот же receive-only тракт HackRF |
| tinySA Basic / Ultra / Ultra+ | один точный `COM<n>` | только bounded metadata Windows; COM не открывается | sweep в dBm после явного подтверждения |
| RTL-SDR | `RTLSDR:<индекс>` | descriptor-only bounded worker | IQ и вычисленный spectrum в dBFS |

Поиск устройства не равен началу приёма. Оператор выбирает найденный кандидат и
отдельно нажимает «Добавить и включить». Неподдерживаемая настройка отклоняется
до capture.

### HackRF One и PortaPack

ALGA VECTOR использует только два официальных host tool:

- `hackrf_info` — descriptor discovery и подтверждение серийного номера;
- `hackrf_transfer` — ограниченный по времени receive-only IQ capture.

Приложение не предоставляет TX API, signal-source mode, antenna port power или
включение RF amplifier. Команда capture явно использует receive mode, отключает
antenna power и RF amp. Уровень показывается в dBFS: абсолютная калибровка dBm
не заявляется.

Поддерживаемый аппаратный профиль HackRF One:

- диапазон настройки: 1 МГц–6 ГГц;
- sample rate: 2–20 MS/s;
- всё выбранное окно должно находиться внутри аппаратного диапазона;
- мгновенная полоса не может превышать sample rate.

PortaPack должен быть вручную переведён в **HackRF USB mode**. Пока
`hackrf_info` не подтвердил устройство и серийный номер, ALGA VECTOR показывает
его как недоступное.

Host tools ищутся в `PATH` или в каталоге `hardware-tools` рядом с
исполняемым файлом/пакетом приложения. Они не входят в Python-зависимость
`.[hardware]`.

### tinySA

Discovery tinySA:

1. читает не более bounded количества системных serial descriptors;
2. показывает явные tinySA-кандидаты и неоднозначные USB Serial/CDC-кандидаты;
3. не открывает, не пишет и не перебирает произвольные COM-порты;
4. требует явного выбора и подтверждения оператора;
5. после открытия выбранного порта сверяет версию/модель и применяет capability.

Поддерживаемые receive-профили:

| Модель | Обычный режим | Явно подтверждённый Ultra mode |
|---|---:|---:|
| tinySA Basic | 0,1–350 МГц | нет |
| tinySA Ultra ZS405 | 0,1–800 МГц | 0,1–5300 МГц |
| tinySA Ultra+ ZS406 | 0,1–900 МГц | 0,1–5400 МГц |
| tinySA Ultra+ ZS407 | 0,1–900 МГц | 0,1–7300 МГц |

Ultra mode не включается программой автоматически. Это swept measurement с
ограничениями подавления зеркал и вероятностью пропуска коротких эпизодов между
точками sweep; он не равен широкополосному real-time IQ-приёму. Harmonic mode
не включён в поддерживаемый профиль ALGA VECTOR.

### RTL-SDR

| Профиль | Поддерживаемая настройка |
|---|---|
| `auto` | безопасный выбор по подтверждённым признакам |
| `generic` | 24–1766 МГц |
| `blog_v4` | 0,5–1766 МГц только после точного EEPROM-подтверждения драйвером |
| `blog_v3_direct_q` | HF через явно доступный direct sampling Q |

USB-строка `Generic RTL2832U OEM` не доказывает модель Blog V4. Ручное имя
профиля также не заменяет hardware confirmation. Стабильная мгновенная полоса
RTL-SDR ограничена 2,56 МГц; широкий диапазон просматривается последовательной
перестройкой центра.

## Автообзор общих диапазонов

Автообзор предлагает общие инженерные участки VHF 30–300 МГц, UHF
300–1000 МГц, L 1–2 ГГц, S 2–4 ГГц, C 4–6 ГГц и составной общий план
30 МГц–6 ГГц. Эти названия не являются сигнатурами источника и не доказывают
назначение наблюдаемого сигнала.

Пункт **«Весь подтверждённый диапазон приёмника»** строится от реального
аппаратного профиля. Для обычного RTL-SDR это 24–1766 МГц; для точно
подтверждённого Blog V4 — 0,5–1766 МГц. Если такой план превышает защитный
лимит окон, интерфейс предлагает выбрать меньший общий участок.

Перед запуском план пересекается с подтверждённым аппаратным профилем.
Неподдерживаемые участки исключаются; если доступных окон нет или их bounded
число превышено, план не запускается. Неизвестная модель не получает
расширенные возможности по предположению.

Один SDR измеряет только одно окно за раз: перестраивается, выдерживает
несколько успешных кадров и затем переходит к следующему окну. Интерфейс
показывает окно последнего принятого кадра, следующую перестройку, прогресс
выдержки и плановый минимум времени цикла. Реальный цикл может быть дольше
из-за скорости устройства. Это не
одновременный широкополосный приём: короткий эпизод между посещениями окна
может быть пропущен.

## Изоляция аппаратного runtime

Реальные адаптеры работают в отдельном Windows worker-процессе, созданном
методом `spawn`. Основной процесс GUI общается с ним через bounded IPC.

Реализованы:

- startup/control/read timeouts;
- фоновый acquisition независимо от UI timer;
- не более одной аппаратной команды в работе и latest-frame handoff без
  бесконечного накопления кадров;
- проверка процесса, snapshot устройства и свежести последнего кадра;
- явный reconnect;
- fail-closed состояние при ошибке host tool, DLL, USB, COM или worker;
- bounded shutdown с принудительным завершением зависшего процесса;
- понятный incident и operator action вместо silent failure.

Отсутствие устройства или optional host tools не должно завершать GUI.

## RF-анализ

Сначала detector проверяет форму кадра, временные метки, непрерывность,
нечисловые значения, stale/dropped frames и шкалу уровня. Затем он оценивает
noise floor, спектральные компоненты, occupied bandwidth и превышение над
фоном.

Temporal FSM использует:

- подтверждение по нескольким согласованным наблюдениям;
- default window 3 из 5 плюс минимальную временную выдержку;
- отдельные attack/release thresholds;
- hysteresis, release hold и debounce;
- component tracking по частоте и полосе;
- сглаживание признаков;
- recurrence и проверку периодичности;
- abstention при недостаточных или ненадёжных данных.

Один импульс или один активный bin не создаёт подтверждённое событие.

Новые безопасные RF-семейства:

- `carrier`;
- `narrowband_burst`;
- `broadband_burst`;
- `packet_like`;
- `voice_like`;
- `periodic_beacon_like`;
- `interference_noise_like`;
- `unknown`.

Каждое решение разделяет:

- качество входных данных;
- эвристическую силу RF-признаков;
- аргументы «за»;
- противоречащие признаки;
- недостающее подтверждение;
- альтернативные общие RF-семейства;
- ограничения выбранного acquisition mode.

Для swept spectrum короткие пропуски не трактуются как доказанное отсутствие
сигнала. Периодический и voice-like класс требуют достаточной временной
структуры; один sweep их не подтверждает.

Компактное уведомление появляется только для alertable-решения в состоянии
`confirmed` или `holding`. Оно описывает общий RF-тип: голосоподобный канал,
пакетоподобный обмен, несущую, ограниченный узкополосный или широкополосный
эпизод, шумоподобную помеху либо неподтверждённый источник. Формулировка
«возможна радиостанция» не является подтверждением рации, а частота и
RF-форма не являются идентификацией физического объекта.

## Направление: fail-closed

Панель «Направление» показывает только угол. Она не содержит карты, дальности
или координат.

Допустимые источники:

- **external** — свежий bearing от внешнего датчика с timestamp, calibration id,
  quality, uncertainty и evidence;
- **manual** — справочная отметка оператора, всегда помеченная как неизмеренная;
- **simulated** — только в явном Demo, всегда с provenance симуляции.

Если валидного источника нет, данные устарели, калибровка не совпадает или
evidence недостаточно, луч скрывается и отображается
**«Направление недоступно»**. Bundled adapter конкретного внешнего DF-сенсора в
0.7.0 не поставляется; реализованы модель, policy gate и runtime ingestion API.

RF-эпизод сам по себе не создаёт bearing.

В этом же разделе показан тренд принятого RF-уровня по нескольким валидным
кадрам одного источника: `РАСТЁТ`, `СТАБИЛЕН`, `ПАДАЕТ` или `НАКОПЛЕНИЕ`.
Это изменение уровня на входе приёмника без пространственной интерпретации.
Рост уровня не доказывает приближение. Постоянное ограничение интерфейса:
**«Расстояние не определяется: RSSI зависит от мощности, антенны и трассы.»**

## SIMPLE MODE, EXPERT MODE и provenance

**SIMPLE MODE** (backward-compatible значение профиля `guided`) показывает:

- что измерено;
- почему система так пишет;
- насколько надёжны данные словесно;
- что не удаётся установить;
- один следующий шаг.

**EXPERT MODE** раскрывает частоту, полосу, level, peak excess,
quality flags, lifecycle, evidence, alternatives и provenance.

Оба режима используют одну измерительную математику и одинаковые защитные
блокировки.

Demo включается только параметром `--demo`. Синтетические данные постоянно
помечаются `simulated`; сохранённый старый профиль не может незаметно включить
Demo при обычном запуске.

## Конфигурация и совместимость

ALGA VECTOR 0.7.0 использует `schema_version: 5`. Старые профили проходят
последовательную миграцию и strict validation. Новые acoustic, airspace и
fusion-поля имеют fail-closed defaults: live-источник не открывается без явной
настройки.

Схема поддерживает:

- `kind: tinysa | rtlsdr | hackrf`;
- точный connection без wildcard;
- `rtlsdr_profile`;
- `tinysa_model`;
- явный `tinysa_ultra_mode`;
- проверку всего частотного окна, а не только central frequency;
- acoustic input mode и локальный путь гражданского ADS-B-контекста;
- параметры temporal fusion;
- live/demo/safe provenance;
- atomic user config и last-known-good fallback.

Поля `map` и `location` остаются в schema v5. Карта доступна только в
EXPERT MODE и сохраняет прежние fail-closed ограничения: локальная база,
картографические измерения и ручная геометрия не становятся координатами
RF-источника. SIMPLE MODE эти технические настройки не показывает.

## Установка среды

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,hardware]"
```

`.[hardware]` устанавливает Python-поддержку serial, RTL-SDR и USB. Для
HackRF отдельно нужны совместимые официальные `hackrf_info` и
`hackrf_transfer`.

Проверка runtime без запуска RF-захвата:

```powershell
.\.venv\Scripts\python.exe -m alga_vector --hardware-preflight
```

Preflight проверяет Python hardware modules, RTL-SDR descriptors, наличие
HackRF host tools и metadata-кандидатов tinySA. Он не запускает spectrum
capture, не меняет частоту и не открывает произвольные COM-порты.

## Запуск

```powershell
# Обычный запуск: live, но только явно включённые адаптеры
.\.venv\Scripts\python.exe -m alga_vector

# Явный live override
.\.venv\Scripts\python.exe -m alga_vector --live

# Без real adapters
.\.venv\Scripts\python.exe -m alga_vector --safe

# Детерминированное обучение с постоянным simulated provenance
.\.venv\Scripts\python.exe -m alga_vector --demo

# Повторить onboarding
.\.venv\Scripts\python.exe -m alga_vector --onboarding
```

Для отдельного каталога данных добавьте:

```powershell
.\.venv\Scripts\python.exe -m alga_vector --safe --data-dir D:\ALGA_DATA
```

## Проверки и сборка Windows

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src\alga_vector
.\.venv\Scripts\python.exe -m pytest --basetemp .build-temp\pytest
.\.venv\Scripts\python.exe -m alga_vector --hardware-preflight
```

PyInstaller onedir без ZIP и installer:

```powershell
.\packaging\build.ps1 -SkipInstaller -SkipPortable
```

Onedir плюс проверенный portable ZIP:

```powershell
.\packaging\build.ps1 -SkipInstaller
```

Полная сборка installer требует установленный Inno Setup 6:

```powershell
.\packaging\build.ps1
```

Build script запускает Ruff, strict Mypy, полный Pytest, source/frozen smokes,
PyInstaller, проверку версий EXE, portable extraction smoke и SHA-256. Флаг
`-SkipTests` допустим только для локальной итерации, не для release gate.

Ожидаемые onedir-файлы:

```text
dist\ALGA VECTOR\ALGA VECTOR.exe
dist\ALGA VECTOR\ALGA VECTOR CLI.exe
dist\ALGA VECTOR\README_FIRST_RU.txt
```

## Хранение и диагностика

- processed-spectrum JSONL с atomic finalize и SHA-256;
- SQLite/WAL-журнал incidents, RF-эпизодов и lifecycle transitions;
- rotating JSONL logs;
- retention только финализированных устаревших записей;
- локальный redacted `.avsupport`;
- без автоматической отправки;
- без raw IQ, spectrum arrays и legacy coordinate data в support bundle.

## Что не заявляется в 0.7.0

- физическая сертификация HackRF, PortaPack, tinySA или RTL-SDR на этом build;
- bundled live-захват с микрофона и полевая валидация акустической модели;
- автоматическое сетевое управление dump1090 или гарантия полноты ADS-B;
- физическая валидация multi-sensor fusion на объекте;
- точность абсолютного уровня HackRF в dBm;
- распознавание конкретного протокола или физического источника;
- RF-пеленгация без отдельного валидированного датчика;
- координаты, дальность, геолокация, идентичность, национальность или IFF;
- гарантия захвата короткого события swept-анализатором;
- hot-unplug/длительный soak на полной матрице реального оборудования;
- Authenticode-подпись, если она не добавлена отдельным release-процессом.

Автоматические тесты используют fake/mock host tools и синтетические кадры.
Перед эксплуатацией конкретная связка Windows, драйвера, firmware, USB,
антенны, аттенюатора и уровня входного сигнала должна пройти отдельный
аппаратный acceptance test.

## Документация

- [`docs/QUICK_START_RU.txt`](docs/QUICK_START_RU.txt)
- [`docs/RELEASE_070_RU.md`](docs/RELEASE_070_RU.md)
- [`docs/BUILD_REPORT_070_RU.md`](docs/BUILD_REPORT_070_RU.md)
- [`docs/SIMPLE_EXPERT_ARCHITECTURE_RU.md`](docs/SIMPLE_EXPERT_ARCHITECTURE_RU.md)
- [`docs/RELEASE_060_RU.md`](docs/RELEASE_060_RU.md) — предыдущий релиз
- [`docs/ALGA_VECTOR_060_FOUNDATION_RU.md`](docs/ALGA_VECTOR_060_FOUNDATION_RU.md)
- [`docs/RELEASE_050_RU.md`](docs/RELEASE_050_RU.md) — исторический релиз
- [`docs/ALGA_CIVIL_RF_050_ARCHITECTURE_RU.md`](docs/ALGA_CIVIL_RF_050_ARCHITECTURE_RU.md)
- [`docs/PRODUCTION_AUDIT_041_RU.md`](docs/PRODUCTION_AUDIT_041_RU.md)

## Официальные аппаратные источники

- [HackRF One documentation](https://hackrf.readthedocs.io/en/stable/hackrf_one.html)
- [HackRF Tools](https://hackrf.readthedocs.io/en/latest/hackrf_tools.html)
- [tinySA model comparison](https://tinysa.org/wiki/pmwiki.php?n=TinySA4.Comparison)
- [tinySA USB interface](https://tinysa.org/wiki/pmwiki.php?n=Main.USBInterface)

## Сторонний ресурс

Golos Text Regular/SemiBold поставляется по SIL Open Font License 1.1.
Лицензия находится в
`src/alga_vector/assets/fonts/OFL-Golos-Text.txt`.
