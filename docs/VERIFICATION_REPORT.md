# ALGA VECTOR — отчёт о верификации

> **Исторический evidence 0.4.x.** Актуальные границы и release gate 0.6.1
> находятся в [RELEASE_061_RU.md](RELEASE_061_RU.md). Числа тестов и
> контрольная сумма ниже относятся только к указанной старой версии.

> Дополнение 0.4.1 от 2026-07-26: RF/SDR hardening, воспроизведённый P0,
> временная FSM и актуальный протокол независимой проверки описаны в
> [PRODUCTION_AUDIT_041_RU.md](PRODUCTION_AUDIT_041_RU.md). Для 0.4.1
> подтверждены Ruff, strict Mypy, 249/249 pytest, frozen GUI/CLI и portable
> smoke; SHA-256 ZIP:
> `B79442F5E17FAF7C781601581B8CE299DF542CBE6C657FB16B697594A3ABE808`.
> Детальные таблицы ниже сохраняют историческое evidence 0.4.0.

Дата документа: 2026-07-25  
Целевая среда: Windows 11 x64, Python 3.12, PySide6 Essentials

Отчёт отделяет автоматизированно проверенное программное поведение от
физической приёмки USB/COM-оборудования.

## Финальный итог тестов

```text
Ruff                           PASS
Mypy strict                    PASS, 68 source files
Pytest                         PASS, 217/217 tests
Hardware descriptor preflight PASS, RTLSDR:0
Source default/live/safe/demo  PASS
Online visible-tile smoke      PASS, one PNG tile
Guided/map visual QA           PASS, 1440×900 and 1120×720
```

Количество подтверждено полным запуском тестов и отдельным
`pytest --collect-only -vv`.

## Подтверждённое состояние

### Продуктовый scope

Рабочие операторские разделы:

- dashboard;
- devices;
- spectrum;
- events;
- map;
- diagnostics;
- settings.

`live` является default-профилем. Safe отключает реальные приёмники, demo
подставляет детерминированные симулированные tinySA/RTL-SDR с явным provenance
`simulated`.

### Config v4

- active schema — `schema_version: 4`;
- допустимы только `tinysa` и `rtlsdr`;
- tinySA требует один connection вида `COM<n>`;
- RTL-SDR требует один connection вида `RTLSDR:<index>`;
- simulated connections разрешены только в demo;
- v1/v2/v3 mappings мигрируют до strict validation;
- legacy RTL span шире sample rate сужается при миграции вместо fallback;
- `rtlsdr_profile` допускает `auto`, `generic`, `blog_v4` и
  `blog_v3_direct_q`;
- весь FFT-window валидируется относительно границ выбранного профиля;
- `map.network_enabled=true`, bounded online cache включён по умолчанию;
- `threshold_dbm` из v2 мигрирует в `spectrum.threshold_level`;
- invalid YAML не перезаписывает active/last-known-good config;
- default — `mode: live`, пустой adapter list и
  `enable_real_adapters: false`.

### Реальные hardware paths

- tinySA serial adapter отправляет команды только в явно настроенный COM-порт
  и возвращает spectrum в dBm;
- RTL-SDR открывает только заданный индекс через `pyrtlsdr/librtlsdr`,
  получает IQ и вычисляет spectrum в dBFS;
- раздел «Устройства» выполняет bounded descriptor-only поиск в отдельном
  worker, не открывает приёмник и требует явного «Добавить и включить»;
- при неуспешном поиске Windows PnP fallback read-only различает Code 28,
  другой problem code, не-WinUSB binding, неполную установку и исправный
  WinUSB при недоступном backend; основной `MI_00` имеет приоритет над
  вспомогательным `MI_01`;
- неверные connection expressions отклоняются;
- отсутствие hardware extras, open/read errors и timeouts преобразуются в
  структурированные diagnostics.

Физический RTL-SDR Blog V4 с тюнером R828D ранее прошёл:

- виден Windows через рабочий интерфейс WinUSB;
- обнаружен исходным и frozen bundled `librtlsdr` как `RTLSDR:0`;
- открыт production hardware worker;
- отдал 4096 корректных IQ samples в низкоуровневом smoke;
- отдал 512-точечный spectrum с provenance `live` и единицей dBFS через
  `ApplicationRuntime`.

При проверке изменений 0.4.0 descriptor-only preflight снова увидел
подключённый `RTLSDR:0`. Повторный exclusive open не выполнялся, потому что
приёмник удерживал уже запущенный экземпляр 0.2.1; это состояние было
диагностировано без завершения пользовательского процесса.

Физический tinySA, hot-unplug и длительный RF/USB soak не выполнялись.

### RF-профили и диапазон RTL-SDR

Покрыты конфигурацией и тестами:

- `auto` выбирает безопасный профиль по доступным признакам;
- `generic` допускает настройку 24–1766 МГц;
- `blog_v4` допускает 0,5–1766 МГц через встроенный upconverter только
  после точного EEPROM-подтверждения backend;
- `blog_v3_direct_q` требует доступного direct sampling Q API;
- дескриптор `Generic RTL2832U OEM` не подтверждает Blog V4; ручной выбор
  модели не форсирует upconverter и при неподтверждённом EEPROM безопасно
  остаётся generic;
- валидируются центральная частота и весь FFT-window;
- стабильная мгновенная полоса ограничена 2,56 МГц;
- полный диапазон просматривается последовательной ручной перестройкой центра
  или пресетами; автоматического sweep/hopping нет.

Физический HF-path 0,5–24 МГц в текущем цикле не открывался: подключённый
приёмник найден, но exclusive open вернул `LIBUSB_ERROR_ACCESS`, пока его
удерживал сторонний SDR-процесс. Программные тесты
подтверждают fail-safe выбор профиля и управление API, но не физическую
чувствительность HF; после закрытия старого приложения нужен hardware smoke.

### Windows spawn worker и IPC

Live real adapters выполняются в отдельном
`multiprocessing.get_context("spawn")` worker. Основной runtime не владеет
serial/native handle.

Покрыты тестами:

- запуск worker и ready handshake;
- IPC round-trip `SpectrumFrame` с NumPy payload;
- startup/control/read deadlines;
- slow worker timeout;
- crash и потеря IPC;
- закрытие Windows pipe между `is_alive()` и `poll()` без выхода
  `BrokenPipeError` в основной runtime;
- terminate/kill зависшего worker;
- fail-closed `FAILED/ERROR` snapshots;
- отсутствие выдачи stale completed frame после failure;
- explicit reconnect с перезапуском worker;
- bounded close;
- safe/no-hardware manager contract.

Дефолтные deadlines:

```text
startup = 5 s
control = 5 s
read spectrum = 15 s
refresh interval = 2 s
```

Это подтверждает реализованную process isolation. Оно не подтверждает
поведение конкретного драйвера при физическом hot-unplug.

### Непрерывный acquisition и честная готовность

- production live real hardware автоматически запускает Qt-независимый
  daemon acquisition-loop с периодом 50 мс;
- refresh выполняется каждые 2 секунды;
- UI/snapshot не инициирует повторную обработку кадра;
- detector и recorder получают каждый уникальный кадр;
- acquisition продолжается без вызовов `snapshot()`;
- settings выполняют bounded pause/swap/restart;
- shutdown будит поток и ограничивает join;
- кадр старше 5 секунд сохраняет provenance и растущий `data_age_ms`, но
  Spectrum capability/readiness становятся blocked/0;
- после read failure UI не показывает `RF-ЯДРО ГОТОВО`;
- acquisition suite 5/5 повторён три раза; зависших процессов не осталось.

Регрессионный сценарий аварийного закрытия worker дополнительно выполнен
10 раз подряд: 10/10 PASS.

### Spectrum, текущая оценка и passive RF events

- live/demo spectrum и waterfall используют явный provenance;
- detector хранит bounded history и ограниченное число sources;
- каждый принятый кадр получает `NO_DATA`, `LEARNING_BACKGROUND`,
  `BACKGROUND_ONLY`, `DATA_UNRELIABLE`, `CONCENTRATED_RF`, `WIDEBAND_RF`,
  `TRANSIENT_BURST` либо `UNCLASSIFIED_RF`;
- до зрелого baseline guided не описывает provisional activity как вывод;
- stale/dropped/gap/clock кадры имеют приоритет `DATA_UNRELIABLE` и не
  обучают фон/persistence/trend;
- expert events остаются narrowband, broadband, transient или unknown;
- компактная in-app панель показывает «возможный узкополосный канал связи»,
  «узкополосная передача», «широкополосная передача или помеха»,
  «короткий пакет или импульсная помеха» либо предупреждение о качестве;
- панель описывает только совместимость формы сигнала и никогда не заявляет,
  что распознан физический дрон, рация или другой объект;
- результат содержит evidence, quality flags и эвристический confidence;
- rising/falling относится только к received-power trend;
- нет идентификации передатчика или объекта;
- нет оценки distance;
- нет вывода об approach;
- нет вычисления azimuth по tinySA или одиночному RTL-SDR;
- нет conflict-specific frequency database;
- SQLite journal хранит system incidents/acknowledgements, а не RF identity
  records.

### Processed-spectrum recording

- формат — `ALGA Spectrum JSONL v1`;
- header содержит `content_kind=processed_spectrum` и `raw_iq=false`;
- активная запись использует `.jsonl.partial`;
- завершение выполняет atomic replace;
- создаётся SHA-256 sidecar;
- abort сохраняет `.partial`;
- retention удаляет только просроченные finalized spectrum captures и их
  checksum-sidecars;
- default retention — 30 дней;
- raw IQ recorder отсутствует.

### Автоматическая карта и raster MBTiles

- экран карты сам запрашивает только тайлы видимого viewport;
- endpoint — точный HTTPS OpenStreetMap URL;
- используется явный `ALGA-VECTOR/0.4.0` User-Agent;
- атрибуция всегда видима на canvas;
- максимум два workers, 2 запроса/с и 48 pending;
- timeout, size/content-type/raster validation и redirect host check;
- disk/memory cache ограничен, TTL 30 дней;
- cache filenames не раскрывают tile coordinates;
- bulk download, prefetch регионов и headless map sweep отсутствуют;
- реальный smoke получил один PNG-тайл, состояние `READY`, cache=1;
- importer проверяет SQLite signature/schema;
- валидируются metadata, zoom, bounds и tile payload;
- принимаются PNG/JPEG/WebP raster tiles;
- пакет копируется атомарно в content-addressed каталог;
- package ID равен SHA-256;
- чтение выполняется read-only;
- tile cache ограничен;
- без сети и без кэша остальные разделы остаются рабочими;
- optional MBTiles имеет приоритет;
- региональный dataset не поставляется.
- fallback без тайлов показывает нейтральную фоновую сетку;
- от базы строятся геодезические кольца кратчайшего расстояния и истинные
  азимутальные спицы;
- клик по карте даёт только картографическое расстояние/азимут;
- ручной луч и условная дальность явно обозначены как ввод оператора;
- stale/conflict/база вне declared coverage блокируют новые расчёты;
- полюса, антимеридиан, zoom 30, off-screen endpoints и антиподальная точка
  покрыты регрессионными тестами.

### GPS/NMEA и защищённая база

- GPS discovery читает только метаданные Windows и не открывает порты;
- NMEA reader открывает только один явный COM-порт с явным baud rate;
- поддерживаются bounded GGA/RMC/GSA sentences с checksum/fix validation;
- статус различает 2D, 3D, отсутствие фиксации, stale и подозрительный скачок;
- подозрительный скачок не переносит базу автоматически;
- доступна ручная точка WGS84 как явно подтверждённый fallback;
- UI предупреждает, что ошибочная ручная база искажает расчёты;
- точная база сохраняется через current-user Windows DPAPI;
- сохранённая координата не возвращается в обычные edit fields;
- точные координаты никогда не включаются в status/логи;
- logs/support redaction удаляет NMEA, coordinate pairs и location fields.

### Guided и expert

- `guided` является default;
- guided dashboard показывает текущую оценку, диапазон, причины, словесное
  качество, отсутствие атрибуции, три шага готовности и одну следующую кнопку;
- guided dashboard скрывает дублирующие плотные таблицы, guided events —
  проценты/raw flags, guided spectrum — tuning/record/technical controls;
- expert показывает расширенную телеметрию, markers и controls;
- оба режима используют один runtime и одинаковые алгоритмы.
- полный MainWindow визуально проверен при 1440×900 и 1120×720; объяснение
  dashboard не сжимается, правая панель карты доступна через прокрутку.

### Support bundle

- `.avsupport` создаётся локально;
- включаются только allowlisted redacted config, device inventory, health,
  incident summary и ограниченные JSONL-логи;
- device IDs псевдонимизируются salted SHA-256 для каждого bundle;
- manifest содержит размер и SHA-256 каждого payload;
- verifier проверяет hashes без извлечения архива;
- raw IQ, raw spectrum arrays и точные координаты исключены;
- автоматической отправки нет.

## Frozen сборка 0.4.0

PyInstaller `onedir` создал:

```text
dist\ALGA VECTOR\ALGA VECTOR.exe
dist\ALGA VECTOR\ALGA VECTOR CLI.exe
```

Windows version resources:

```text
GUI ProductName/FileVersion       ALGA VECTOR / 0.4.0.0
CLI ProductName/FileVersion       ALGA VECTOR / 0.4.0.0
```

Version resource-файлы и оба EXE проверены. Сборка зафиксировала:

```text
frozen CLI hardware preflight
frozen CLI default/live/safe/demo
frozen GUI default/safe
legacy user profile -> live/SIM=0
ZIP extract + CLI preflight/safe
README_FIRST_RU.txt packaged
```

GUI-проверка выполнялась как отдельный процесс PowerShell с ожиданием
завершения, поэтому exit code относится к frozen GUI executable.

Проверенный переносимый пакет:

```text
dist\ALGA_VECTOR-0.4.0-Windows-x64-onedir.zip
Размер: 63 822 315 байт
SHA-256: 049FDBC19C4E49F450551EB881CDA79C0FA6A81756A3B331599C26FEFD1ECDF5
Контрольная сумма: `dist\ALGA_VECTOR-0.4.0-Windows-x64-onedir.zip.sha256.txt`
```

## Команды воспроизведения

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,hardware]"

.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src\alga_vector
.\.venv\Scripts\python.exe -m pytest --basetemp .build-temp\pytest
.\packaging\build.ps1 -SkipInstaller
```

Hardware preflight проверяет импорты `serial`, `rtlsdr`, `usb` и
descriptor-only RTL-SDR discovery, но не открывает устройство.

## Не выполнено и не заявляется

- физическая совместимость с tinySA и RTL-SDR, кроме проверенного Blog V4;
- расширенная матрица firmware, USB topology и Windows drivers;
- absolute RF calibration/accuracy;
- raw IQ recording;
- идентификация источника, distance или approach;
- азимут без отдельного проверенного направленного/когерентного тракта;
- conflict-specific frequency database;
- встроенный региональный набор карт;
- SLA публичного tile service или разрешённый bulk/offline-prefetch;
- hot-unplug/soak/disk-full на физическом потоке;
- полный install/repair/uninstall цикл на чистой Windows VM;
- Inno Setup installer build;
- Authenticode signing GUI, CLI и installer;
- финальный release-grade SBOM и dependency lock audit.

До физической приёмки и закрытия installer/signing gates результат является
engineering build, а не production hardware release.
