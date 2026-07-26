# ALGA VECTOR 0.5.0 — отчёт о релизе

Дата документа: 26 июля 2026 года.

Подпись продукта: **«Разработал: Буйвол и Задира»**.

## Итог

ALGA VECTOR 0.5.0 — production-oriented Windows desktop build для законного
пассивного RF-мониторинга и лабораторного анализа. Основной пользовательский
workflow теперь строится только вокруг измеряемых RF-данных:

```text
приёмник → измеренный кадр → quality gate → temporal decision → RF-событие
```

Направление вынесено в отдельный optional-контур. Без валидного источника
bearing система показывает `unavailable` и не пытается заменить измерение
расчётом по мощности.

Карта и GPS удалены из видимой навигации, onboarding, быстрого запуска и
рабочих настроек. Legacy `map/location` остаются compatibility-кодом schema v4,
но отключены в стандартном v0.5.0 composition/workflow, чтобы старые профили
продолжали читаться без повреждения.

## Исправлено

### Достоверность интерфейса

- Удалена зависимость RF-готовности от базы, GPS или карты.
- Маршрут `map` заменён на `direction`.
- Убраны формулировки, обещавшие идентификацию физического объекта.
- При отсутствии измеренного кадра live-поток не подменяется Demo.
- Confidence явно подписан как эвристическая сила признаков, а не вероятность.
- Уровень HackRF обозначается dBFS; абсолютный dBm не заявляется.
- Peak excess обозначается как превышение над фоном/SNR-подобный показатель,
  если нет калиброванного SNR.

### Направление

- Добавлена отдельная модель `DirectionObservation` и policy gate.
- Manual bearing всегда помечается как введённая оператором неизмеренная
  отметка.
- Simulated bearing разрешён только в явном Demo.
- External bearing принимается только со свежим timestamp, uncertainty,
  quality, calibration id и evidence.
- Stale, weak, uncalibrated или отсутствующий источник скрывает активный луч.
- RF-уровень одиночного приёмника не преобразуется в bearing.
- Координаты, дальность и карта отсутствуют в новой панели.

### RF-классификация

- Основные выходные классы ограничены нейтральными RF-семействами:
  `carrier`, `narrowband_burst`, `broadband_burst`, `packet_like`,
  `voice_like`, `periodic_beacon_like`, `interference_noise_like`, `unknown`.
- Один импульс не подтверждает событие.
- Подтверждение требует временной поддержки, default 3 из 5 и dwell.
- Добавлены recurrence и проверка periodicity минимум по трём циклам.
- Добавлены hysteresis, release hold, temporal smoothing и debounce.
- Переключение семейства требует повторного подтверждения.
- При недостатке признаков classifier воздерживается и возвращает `unknown`.
- Legacy enum-значения сохранены только для чтения старых journal rows.

### Аппаратный слой

- `AdapterConfig.kind` поддерживает `tinysa`, `rtlsdr` и `hackrf`.
- Для каждого адаптера используется hardware capability вместо глобального
  вымышленного диапазона UI.
- Проверяется всё частотное окно, а не только central frequency.
- HackRF connection требует точный `HACKRF:<hex serial>`.
- tinySA требует один точный `COM<n>` и отдельную модель/Ultra confirmation.
- Неподдерживаемые частота, полоса или sample rate блокируются до capture.

## Улучшено

### HackRF One / PortaPack

- Discovery выполняется официальным `hackrf_info` с timeout и bounded output.
- Capture выполняется только `hackrf_transfer -r -`.
- IQ — signed 8-bit quadrature, bounded по числу байтов и времени.
- TX API, signal-source mode, antenna power и RF amp enable отсутствуют.
- `-a 0` и `-p 0` фиксируют безопасный receive contract.
- Host tools ищутся в `PATH` либо каталоге `hardware-tools`.
- PortaPack поддерживается только после ручного перехода в HackRF USB mode.
- Поддерживаемая capability: 1 МГц–6 ГГц, 2–20 MS/s.

### tinySA

- Discovery читает только bounded serial metadata и не открывает COM.
- Неоднозначный USB Serial/CDC-кандидат требует ручной сверки.
- Порт открывается только после явного действия пользователя.
- Поддерживаются profiles Basic, Ultra ZS405, Ultra+ ZS406 и ZS407.
- Ultra mode не включается автоматически и имеет отдельное предупреждение о
  swept/alias limitations.
- Harmonic mode не объявлен поддерживаемым.

### RTL-SDR

- Сохранён descriptor-only bounded discovery.
- Сохранены профили `auto`, `generic`, `blog_v4`, `blog_v3_direct_q`.
- Blog V4 HF требует подтверждения backend/EEPROM.
- Мгновенная полоса отделена от полного диапазона перестройки.

### UX

- Onboarding состоит из шести этапов:
  welcome → experience → storage → receiver → interpretation/limits → finish.
- Guided показывает объяснение, ограничения и один следующий шаг.
- Expert показывает tuning, quality flags, lifecycle, evidence и alternatives.
- Dashboard checklist:
  receiver → measured frame → interpretation/events.
- Direction optional и не влияет на готовность основного RF-контура.
- Добавлены понятные пустые состояния и actionable incidents.
- Demo provenance остаётся видимым на всех экранах.

### Диагностика

- Hardware preflight сообщает состояние обязательных Python-модулей,
  RTL-SDR descriptors, HackRF host tools и tinySA metadata candidates.
- Optional hardware failure не превращается в crash основного GUI.
- Structured logs и support bundle сохраняют redaction.
- Support bundle исключает raw IQ, spectrum arrays и legacy coordinate data.

## Оптимизировано

- Hardware subprocess запускается без shell, stdin и видимого console window.
- Output host tools и время выполнения ограничены.
- Реальные адаптеры изолированы в отдельном Windows worker-процессе.
- Acquisition работает независимо от UI refresh timer.
- В worker допускается только одна незавершённая команда, а runtime хранит
  последний принятый кадр вместо неограниченной очереди.
- Повторная обработка одного sequence исключена.
- Robust Welch остаётся базой IQ spectrum вместо single FFT/max-pooling.
- Temporal tracker ограничен числом источников и tracks на источник.
- Family debounce уменьшает churn классификации.
- Episode debounce уменьшает повторные уведомления.
- SQLite/WAL сохраняет RF-эпизоды и lifecycle transitions без блокировки UI.
- Settings применяет device-driven limits и не отправляет legacy map/location
  payload.

## Совместимость

Активная конфигурационная схема остаётся **`schema_version: 4`**.

Это намеренное решение:

- новые поля адаптеров имеют безопасные defaults;
- старые v1/v2/v3 профили уже мигрируют в v4;
- повышение номера схемы не требуется для скрытия UI-функции;
- `map` и `location` сохраняются для десериализации старых профилей;
- новая навигация, onboarding и Settings их не используют;
- legacy-модули и их тесты можно удалить только отдельной миграцией данных.

Сохранённые старые координаты не должны возвращаться в обычный UI, status,
логи или support bundle.

## Проверено автоматически

Для текущего дерева исходников выполнены:

- полный Pytest suite: **298 passed**;
- все UI-тесты в offscreen Qt;
- Ruff по `src` и `tests`: **All checks passed**;
- strict Mypy по `src/alga_vector`: **78 source files, no issues**;
- четыре source headless smoke в default-live/live/safe/demo с exit code 0;
- hardware preflight без spectrum capture;
- проверки fail-closed Direction;
- fake/mock HackRF host tools;
- metadata-only tinySA discovery;
- capability validation для HackRF, tinySA и RTL-SDR;
- legacy map/location module tests.

Автоматический PASS не является физической проверкой приёмника.

Фактический preflight в среде этой проверки подтвердил Python-модули
`pyserial`, `pyrtlsdr` и `pyusb`. RTL-SDR не был подключён, `hackrf_info` и
`hackrf_transfer` отсутствовали, metadata-кандидаты tinySA не найдены. Поэтому
результат подтверждает только корректный fail-closed software path, а не
работоспособность реального оборудования.

## Не проверено физически

В рамках этого релизного цикла не заявляются:

- подключение реального HackRF One;
- PortaPack в HackRF USB mode;
- реальный IQ capture через установленный vendor host tool;
- подключение реального tinySA Basic/Ultra/Ultra+;
- проверка firmware/model detection на ZS405/ZS406/ZS407;
- полная матрица RTL-SDR revisions и Windows drivers;
- hot-unplug/reconnect на реальном USB;
- длительный hardware soak;
- чувствительность, абсолютная амплитудная точность или probability of detection;
- сертификация внешнего DF-сенсора;
- чистая Windows VM и Authenticode, пока это не подтверждено отдельным build log.

## Оставшиеся риски

### Измерительные

- Swept tinySA может пропустить короткий эпизод между sweep-точками.
- Ultra mode имеет alias/image suppression и ухудшение измерительной
  достоверности на верхних частотах.
- HackRF dBFS зависит от gain, антенны, тракта и внешних фильтров.
- Один spectrum receiver не даёт идентичность или bearing.
- Общие RF-семейства требуют полевой валидации на размеченном гражданском
  лабораторном dataset.

### Аппаратные

- Несовместимые версии `hackrf_info`/`hackrf_transfer` могут изменить вывод
  или поведение.
- PortaPack вне HackRF USB mode не обнаруживается.
- USB Serial metadata может не содержать название tinySA.
- Неверно выбранный неоднозначный COM-порт будет отклонён при probe/open, но
  требует внимания оператора.
- Устройство может быть занято другим SDR-приложением.

### Release

- PyInstaller onedir не равен installer acceptance на чистой машине.
- Неподписанный EXE может вызывать предупреждение Windows.
- Portable ZIP должен быть проверен после распаковки и иметь SHA-256.
- Build с `-SkipTests` не является релизным.

## Как проверить релиз

### 1. Source gate

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src\alga_vector
.\.venv\Scripts\python.exe -m pytest --basetemp .build-temp\pytest
.\.venv\Scripts\python.exe -m alga_vector --hardware-preflight
```

Ожидается нулевой exit code каждой команды.

### 2. Режимы без оборудования

```powershell
.\.venv\Scripts\python.exe -m alga_vector --headless-smoke --skip-onboarding
.\.venv\Scripts\python.exe -m alga_vector --live --headless-smoke --skip-onboarding
.\.venv\Scripts\python.exe -m alga_vector --safe --headless-smoke --skip-onboarding
.\.venv\Scripts\python.exe -m alga_vector --demo --headless-smoke --skip-onboarding
```

Проверить:

- обычный/live запуск не создаёт simulated data;
- safe не открывает real adapters;
- demo постоянно маркируется simulated;
- отсутствие устройств не завершает GUI;
- в навигации нет карты;
- Direction без источника показывает unavailable.

### 3. Windows build

Onedir без portable/installer:

```powershell
.\packaging\build.ps1 -SkipInstaller -SkipPortable
```

Onedir и portable ZIP:

```powershell
.\packaging\build.ps1 -SkipInstaller
```

Полный installer:

```powershell
.\packaging\build.ps1
```

Build считается релизным только после frozen GUI/CLI smokes, проверки
FileVersion/ProductVersion, распаковки portable ZIP и сверки SHA-256.

### 4. Отдельный hardware acceptance

Для каждого реального устройства зафиксировать:

- модель и serial;
- firmware;
- Windows version;
- driver/host tools version;
- USB controller/port;
- антенну, фильтры и аттенюатор;
- выбранные center/span/sample rate;
- 30-минутный стабильный приём;
- reconnect после штатного отключения;
- controlled hot-unplug;
- отсутствие TX/antenna-power действий;
- raw build log и redacted support bundle.

Hardware acceptance не должен выполняться на неизвестном или небезопасном
входном уровне.

## Официальные источники аппаратных ограничений

- [Great Scott Gadgets: HackRF One](https://hackrf.readthedocs.io/en/stable/hackrf_one.html)
- [Great Scott Gadgets: HackRF Tools](https://hackrf.readthedocs.io/en/latest/hackrf_tools.html)
- [tinySA model comparison](https://tinysa.org/wiki/pmwiki.php?n=TinySA4.Comparison)
- [tinySA USB interface](https://tinysa.org/wiki/pmwiki.php?n=Main.USBInterface)
