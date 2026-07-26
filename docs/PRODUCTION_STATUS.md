# ALGA VECTOR — фактический статус

> **Исторический документ 0.4.x.** Актуальное состояние 0.6.1 и его
> release-гейты описаны в [RELEASE_061_RU.md](RELEASE_061_RU.md). Изменения
> 0.5.0, удаление карты/GPS из рабочего интерфейса, Direction fail-closed и
> аппаратная матрица описаны в [RELEASE_050_RU.md](RELEASE_050_RU.md) и
> [ALGA_CIVIL_RF_050_ARCHITECTURE_RU.md](ALGA_CIVIL_RF_050_ARCHITECTURE_RU.md).

> Актуализация 0.4.1 от 2026-07-26: этот документ сохраняет подтверждённое
> состояние 0.4.0. Delta-аудит RF/SDR-контура, новые ограничения и незакрытые
> release-гейты находятся в
> [PRODUCTION_AUDIT_041_RU.md](PRODUCTION_AUDIT_041_RU.md). Наличие temporal
> RF-классификации не является идентификацией физического объекта. Программный
> release gate 0.4.1 пройден: 249/249, Ruff/Mypy, frozen GUI/CLI и portable
> smoke; физический hardware/field/soak gate остаётся открытым.

Дата среза: 2026-07-25

## Вывод

ALGA VECTOR 0.4.0 — рабочая Windows desktop engineering build для пассивного
RF-мониторинга с реальным UI, хранением, автоматической картой с локальным
кэшем, NMEA GPS, device-aware настройкой и безопасным автопоиском RTL-SDR.

Изоляция live hardware реализована: реальные tinySA/RTL-SDR работают в
отдельном Windows `spawn` worker через IPC. Timeout и crash переводят
приёмники в fail-closed состояние, а reconnect перезапускает worker. Этот
контур покрыт тестами. Физический RTL-SDR Blog V4 (`R828D`) на Windows 11
прошёл descriptor discovery, open и получение live-spectrum в dBFS.
Физический tinySA, расширенная hardware-матрица и RF accuracy не проверены.

## Матрица текущих возможностей

| Область | Текущее состояние | Граница |
|---|---|---|
| UI | Dashboard, devices, spectrum, events, map, diagnostics, settings | Русский-first; guided/expert; проверено 1120×720 |
| Режим запуска | `live` по умолчанию | Реальное железо включается только явно |
| tinySA | Реальный serial spectrum adapter | Один connection вида `COM7` |
| RTL-SDR | Descriptor-only автопоиск, Windows PnP fallback, реальный IQ adapter, device-aware tuning и software FFT | Физический Blog V4 ранее подтверждён как `RTLSDR:0` |
| Hardware process | Windows `spawn` worker + IPC | Blog V4 проверен; tinySA и hot-unplug не проверены |
| Failure policy | Startup/control/read deadlines, fail-closed, reconnect | Не заменяет hot-unplug/soak |
| Учебный режим | Детерминированные симулированные tinySA и RTL-SDR | Всегда `simulated` |
| Спектр | Live/demo spectrum, waterfall, quality/provenance | Absolute calibration не заявлена |
| Acquisition | Фоновый цикл 50 мс для live real hardware | UI читает latest frame; stale >5 с блокирует readiness |
| RF-оценка | Assessment каждого кадра + объяснимые in-app signal-family alerts | Нет identity, distance, approach или RF-azimuth |
| Журнал | SQLite/WAL incidents и acknowledgements | Нет conflict-specific frequency DB |
| Запись | Atomic processed-spectrum JSONL + SHA-256 | Не raw IQ |
| Карта | Видимые OSM-тайлы, bounded cache, геодезические кольца/азимуты; optional MBTiles | Картографическая/ручная геометрия, не RF-локализация |
| Координаты | GPS/NMEA GGA/RMC/GSA или подтверждённая ручная WGS84 | Явные GPS COM/baud; metadata-only discovery |
| Защита базы | Current-user Windows DPAPI | Точка не переносится между пользователями |
| Retention | Очистка finalized spectrum captures | `.partial` сохраняются |
| Support bundle | Локальный redacted `.avsupport` | Нет raw arrays и точных координат |
| Config | Strict schema v4 и миграция v1/v2/v3 | Invalid config уходит в fallback |
| Frozen build | PyInstaller `onedir`, GUI + CLI, Windows version metadata 0.4.0.0 | ZIP/SHA и распакованная копия проверены |

## Hardware worker

Live runtime выбирает отдельный hardware manager, когда:

- профиль находится в режиме `live`;
- `devices.enable_real_adapters=true`;
- есть хотя бы один включённый tinySA или RTL-SDR с несимулированным
  connection.

Worker владеет serial/native handles и принимает versioned IPC-команды
`refresh`, `read_spectrum`, `reconnect` и `close`.

Отдельный daemon acquisition-loop основного runtime обращается к
неблокирующему proxy каждые 50 мс и делает refresh каждые 2 секунды. Поэтому
приём, generic detector и processed-spectrum recorder продолжают работать,
даже если Qt временно занят. UI раз в секунду строит состояние из последнего
кадра и не запускает повторную обработку. После 5 секунд без свежего
валидного кадра capability/readiness блокируются и создаётся incident.

Дефолтные бюджеты:

| Операция | Deadline |
|---|---:|
| Запуск worker | 5 с |
| Control RPC | 5 с |
| Spectrum read | 15 с |
| Периодический refresh | 2 с |
| Join после terminate/kill | 0,5 с |

После timeout, потери IPC или crash основной runtime:

1. закрывает IPC;
2. завершает либо принудительно убивает worker;
3. очищает незавершённый spectrum result;
4. публикует `FAILED/ERROR` snapshots для включённых приёмников;
5. не продолжает использовать stale frame как live;
6. допускает явный reconnect, создающий новый worker.

Тестовые сценарии проверяют round-trip spectrum frame через `spawn`, slow
worker timeout, process crash, fail-closed snapshots, reconnect и bounded
shutdown. Отдельно проверена гонка закрытия Windows pipe между проверкой
процесса и `poll()` — ошибка преобразуется в fail-closed состояние; повторный
регрессионный прогон 10/10 PASS. Дополнительно реальный Blog V4 прошёл
штатный worker path и отдал spectrum из 512 точек с provenance `live`.

## Автообнаружение RTL-SDR

Экран «Устройства» предоставляет отдельный безопасный поток:

1. «Найти RTL-SDR» запускает disposable `spawn` worker;
2. worker читает только count/name/USB strings через bundled `librtlsdr`;
3. приёмник не открывается, частота и gain не меняются;
4. UI показывает только product и `RTLSDR:<index>`, без serial/manufacturer;
5. «Добавить и включить» повторно проверяет присутствие, атомарно сохраняет
   профиль и запускает штатный hardware worker.

Поиск ограничен 16 устройствами, имеет 4-секундный timeout и
terminate/kill fallback. Повторное добавление идемпотентно; safe/demo
отклоняют активацию реального оборудования.

Если `librtlsdr` не возвращает устройство, read-only Windows fallback через
SetupAPI/CfgMgr32 проверяет только present PnP records. Он не открывает
приёмник и не меняет систему, но различает Code 28, другие PnP errors,
не-WinUSB binding, неполную запись драйвера и исправный WinUSB при
недоступном backend. Для составного RTL2832U приоритет имеет рабочий `MI_00`,
поэтому ошибка вспомогательного `MI_01` не создаёт ложный диагноз.

## RF-профили и доступный диапазон

Настройка RTL-SDR учитывает профиль конкретного адаптера:

| Профиль | Границы настройки | Условие |
|---|---:|---|
| `auto` | по безопасно определённому профилю | default |
| `generic` | 24–1766 МГц | обычный RTL-SDR |
| `blog_v4` | 0,5–1766 МГц | только после точного EEPROM-подтверждения драйвером |
| `blog_v3_direct_q` | HF direct sampling Q | только при доступном API |

Дескриптор `Generic RTL2832U OEM` недостаточен для автоматического
подтверждения Blog V4. В этом случае `auto` выбирает безопасный `generic`.
Ручной `blog_v4` хранит ожидаемую модель, но не форсирует аппаратный тракт:
если backend не прочитал точные EEPROM-строки `RTLSDRBlog / Blog V4`,
runtime оставляет 24–1766 МГц, отключает HF и показывает причину.

Валидируется весь FFT-window. Стабильная мгновенная полоса RTL-SDR — не более
2,56 МГц; она не равна полному диапазону тюнера. Другой участок выбирается
пресетом либо ручной перестройкой центральной частоты. Автоматического
sweep/hopping нет: последовательная ручная перестройка не смешивает baseline,
persistence и trend разных участков спектра.

## Конфигурация v4

`schema_version: 4`:

- разрешает только `tinysa` и `rtlsdr`;
- требует точный `COM<n>` или `RTLSDR:<index>`;
- не допускает `SIM:*` вне demo;
- для включённого RTL-SDR требует `span_hz <= sample_rate_hz`;
- проверяет весь FFT-window в границах выбранного `rtlsdr_profile`;
- сохраняет `auto`, `generic`, `blog_v4` или `blog_v3_direct_q`;
- при миграции старого RTL-профиля сужает несовместимую полосу до sample rate;
- использует `spectrum.threshold_level`;
- добавляет `map.network_enabled` и bounded `online_cache_mib`;
- мигрирует поддерживаемые v1/v2/v3 mappings до strict Pydantic validation;
- сохраняет user config и last-known-good через временные файлы, `fsync` и
  atomic replace.

Default:

```yaml
schema_version: 4
mode: live
devices:
  enable_real_adapters: false
  adapters: []
map:
  network_enabled: true
  online_cache_mib: 256
```

То есть приложение загружается в live-контексте, но не открывает железо до
явной настройки.

## RF-события и данные

Для каждого принятого кадра публикуется одно состояние:

- `NO_DATA`;
- `LEARNING_BACKGROUND`;
- `BACKGROUND_ONLY`;
- `DATA_UNRELIABLE`;
- `CONCENTRATED_RF`;
- `WIDEBAND_RF`;
- `TRANSIENT_BURST`;
- `UNCLASSIFIED_RF`.

Компактная in-app панель преобразует эти признаки в понятные семейства:

- возможный узкополосный канал связи;
- узкополосная передача;
- широкополосная передача или помеха;
- короткий пакет или импульсная помеха;
- ненадёжные данные.

Формулировка всегда описывает совместимость формы сигнала. Панель никогда не
утверждает, что физически распознаны рация, дрон или другой объект, и по клику
открывает события с измеримыми причинами.

Сначала накапливается фон; до его зрелости guided не делает вывод о форме
изменения. Stale/dropped/gap/clock кадры сразу получают `DATA_UNRELIABLE` и
не обучают фон, persistence или trend. Expert events сохраняют measured
evidence, quality flags и эвристическую оценку признаков. Изменение принятого
уровня является received-power trend и не означает изменение расстояния.

Нет:

- идентификации источника или объекта;
- оценки расстояния;
- вывода о приближении;
- вычисления азимута по tinySA или одиночному RTL-SDR;
- conflict-specific частотной базы;
- долговременной базы RF-идентичностей.

Направление требует отдельного проверенного направленного либо когерентного
приёмного тракта. Текущая сборка не рисует неподтверждённые RF-секторы или
цели на карте.

SQLite/WAL используется для системных incidents и acknowledgements. Текущий
список RF events ограничен памятью runtime.

## Запись и retention

Recorder сохраняет обработанные кадры:

```text
format = ALGA Spectrum JSONL v1
content_kind = processed_spectrum
raw_iq = false
```

Активная запись имеет `.jsonl.partial`. Штатная финализация выполняет atomic
rename и создаёт `.sha256`. Abort сохраняет `.partial`.

Retention по умолчанию — 30 дней. Он удаляет только просроченные
финализированные `alga-spectrum-*.jsonl` и их точные checksum-sidecars.

## Автоматическая карта, MBTiles и приватная база

Обычный пользователь не импортирует карту вручную. При открытии экрана
canvas запрашивает только видимые тайлы у
`https://tile.openstreetmap.org/{z}/{x}/{y}.png`:

- HTTPS и host/redirect validation;
- отдельный стабильный User-Agent `ALGA-VECTOR/0.4.0`;
- видимая атрибуция `© OpenStreetMap contributors · ODbL`;
- не более двух workers и 2 запросов/с;
- очередь не более 48 запросов;
- timeout, максимальный размер и проверка PNG/JPEG/WebP magic;
- 30-дневный bounded memory/disk cache с hashed filenames;
- экспоненциальный backoff и ручной retry;
- ни одного API для region prefetch/bulk download;
- tile coordinates и точная база не попадают в логи/support bundle.

Первый реальный сетевой smoke получил один PNG-тайл и подтвердил состояние
`READY`. Public tile service best-effort, без SLA.

Raster MBTiles importer остаётся рабочим экспертным вариантом:

- проверяет SQLite signature/schema;
- валидирует metadata, zoom, bounds и raster payload;
- принимает PNG/JPEG/WebP tiles;
- копирует пакет атомарно;
- использует SHA-256 content address;
- открывает каталог read-only;
- ограничивает tile cache.

Локальный MBTiles имеет приоритет. Региональный dataset в поставку не входит.

GPS discovery читает только метаданные Windows и не открывает найденные
порты. Оператор явно выбирает один `COM<n>` и baud rate. Reader принимает
bounded NMEA GGA/RMC/GSA и различает 2D, 3D, отсутствие фиксации, stale и
подозрительный скачок; скачок не переносит базу автоматически.

При отсутствии GPS оператор может явно подтвердить ручную базу WGS84. Это
fallback с предупреждением: неверное положение базы делает расстояния и
азимуты некорректными. Точная координата шифруется Windows DPAPI для текущего
пользователя, не возвращается в обычные поля UI, никогда не пишется в
status/логи и исключается из support bundle.

От базы строятся геодезические кольца кратчайшего расстояния и истинные
азимутальные спицы. Доступны обзор, измерение точки карты и ручной луч с
необязательной условной дальностью. Ручная база помечается как непроверенная;
stale/conflict/база вне declared bounds блокируют новые расчёты. Антиподальная
точка сохраняет расстояние, но не получает выдуманный азимут. Полюса, zoom 30,
антимеридиан и дальние off-screen endpoints покрыты регрессионными тестами.

## Guided и expert

Оба режима используют одинаковый runtime:

- guided показывает текущую оценку, диапазон, измеримые причины, качество
  данных словами, явный ответ об отсутствии атрибуции, три шага готовности и
  одну следующую кнопку;
- guided dashboard скрывает дублирующие плотные таблицы, а guided spectrum —
  tuning/record/technical controls;
- expert показывает дополнительные device columns, spectrum controls,
  markers и quality flags;
- expert не меняет алгоритм, confidence или достоверность результата.

## Support bundle

Локальный `.avsupport` содержит allowlisted:

- build/runtime metadata;
- redacted effective config;
- псевдонимизированный device inventory;
- health snapshot;
- summary incidents;
- ограниченные redacted JSONL-логи;
- manifest с размером и SHA-256 каждого payload.

Raw IQ, spectrum arrays, NMEA, точные координаты, secrets, network identifiers
и локальные пути исключаются. Автоматической отправки нет.

## Build status 0.4.0

PyInstaller `onedir` собрал:

```text
dist\ALGA VECTOR\ALGA VECTOR.exe
dist\ALGA VECTOR\ALGA VECTOR CLI.exe
```

Version resources проверены: `ProductName=ALGA VECTOR`,
`FileVersion`/`ProductVersion` 0.4.0.0 / 0.4.0. Архив занимает
63 822 315 байт; sidecar совпадает с повторно вычисленным SHA-256.

```text
dist\ALGA_VECTOR-0.4.0-Windows-x64-onedir.zip
SHA-256: 049FDBC19C4E49F450551EB881CDA79C0FA6A81756A3B331599C26FEFD1ECDF5
```

После сборки повторно пройдены:

- frozen CLI hardware preflight;
- frozen CLI default-live, explicit-live, safe и demo headless smoke;
- frozen GUI default-live и safe через PowerShell `Start-Process`, exit code 0;
- ZIP extract + CLI hardware preflight/safe smoke;
- default launch на существующем legacy demo-профиле: `mode=live`, SIM=0.

Hardware preflight выполняет bundled descriptor-only discovery. Физический
Blog V4 ранее прошёл descriptor/open/live spectrum в production worker.
Текущий descriptor-only preflight видит `RTLSDR:0`; повторный exclusive open
вернул `LIBUSB_ERROR_ACCESS`, пока устройство удерживал сторонний SDR-процесс.
Перед запуском 0.4.0 следует закрыть старую ALGA VECTOR и другой SDR-софт.

## Открытые release-гейты

- [x] итоговый test count: 217/217 PASS;
- [x] Ruff PASS; strict Mypy PASS по 68 source files;
- [x] guided/map visual QA на 1440×900 и 1120×720;
- [x] frozen CLI/GUI smoke, ZIP extract и portable smoke;
- [x] SHA-256 sidecar повторно проверен;
- [x] физический smoke RTL-SDR Blog V4: discovery/open/live spectrum;
- [ ] расширенная матрица tinySA/RTL-SDR: модели, firmware, Windows drivers;
- [ ] hot-plug/hot-unplug и reconnect на физических устройствах;
- [ ] disk-full/slow-disk и длительный soak с реальным потоком;
- [ ] clean Windows VM: install, offline first run, repair, uninstall;
- [ ] Inno Setup installer собран и проверен;
- [ ] Authenticode signing EXE/installer с timestamp;
- [ ] dependency lock/hashes, SBOM и финальная проверка third-party notices.

До физической приёмки и закрытия installer/signing gates сборку следует
называть engineering build, а не production hardware release.

## Подготовка среды

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,hardware]"
```
