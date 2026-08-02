# ALGA VECTOR — версии и артефакты

Этот файл фиксирует только версии, для которых в рабочем дереве найдены
исполняемые артефакты или отдельное release evidence. Он не восстанавливает
отсутствующую историю догадками.

Актуальный release candidate: **1.0.0rc2**.
Актуальная стабильная рекомендуемая версия до завершения аппаратной приёмки:
**0.7.0**.

## Сводная таблица

| Версия | Статус | Найденный артефакт/evidence | Главное изменение |
|---|---|---|---|
| 1.0.0rc2 | Предварительная | Проверенный portable ZIP, checksum, release/build reports | Восстановленный live RF-тракт, capability-driven startup scan, generic fallback и полевой debug |
| 1.0.0rc1 | Предварительная | Проверенный portable ZIP, checksum, release/build reports | Target-centric backend, новый SIMPLE MODE, sensor readiness, EXPERT «Цели» |
| 0.2.1 | Историческая | Legacy portable ZIP, checksum и GUI/CLI с PE version `0.2.1` | Ранний запускаемый Windows-инкремент |
| 0.3.0 | Историческая | Portable ZIP, checksum, распакованные GUI/CLI, quick start | Live по умолчанию, RTL-SDR discovery, карта/база, novice/expert |
| 0.4.0 | Историческая | Portable ZIP, checksum, verification report | Hardware worker, config v4, RTL-SDR/tinySA, spectrum/events/map/diagnostics |
| 0.4.1 | Историческая | Portable ZIP, checksum, production audit | Temporal RF FSM, подавление одиночных всплесков, hardening acquisition |
| 0.5.0 | Историческая | Portable ZIP, checksum, release report | Receive-only RF workflow, HackRF/tinySA capabilities, fail-closed Direction |
| 0.6.0 | Историческая | Portable ZIP, checksum, release/build reports | Первый запускаемый мультисенсорный foundation |
| 0.6.1 | Историческая | Portable ZIP, checksum, release/build reports | Последовательный автообзор и объяснимые RF-уведомления |
| 0.7.0 | Стабильная рекомендуемая до аппаратной приёмки RC | Portable ZIP, checksum, release/build reports | Один backend, SIMPLE/EXPERT, нормализованный event bus и operator situation |

В доступном дереве **не найдено** артефакта, version metadata или release
evidence для `0.3.1`. Поэтому такая версия не включена в список выпущенных.

## 1.0.0rc2 — восстановление полевого RF-тракта

Дата кандидата: 2 августа 2026 года.

Главное:

- аппаратный статус `STREAMING` появляется только после принятого кадра;
- startup incident и reason code объясняют конкретный отказ открытия RTL-SDR;
- первый Live-запуск включает bounded `field_priority`, ограниченный реальными
  возможностями выбранного приёмника;
- schema обновлена до `schema_version: 7`, добавлен управляемый preset
  `detection_sensitivity`;
- каждый energy-gate candidate синхронно попадает в Event Bus как generic
  `RADIO_ACTIVITY_DETECTED`, даже если classifier не знает форму;
- SIMPLE MODE больше не скрывает low-confidence generic RF-активность;
- добавлены сквозные DEBUG-события DEVICE → CAPTURE → DETECTOR → EVENT BUS → UI;
- RF-only наблюдение по-прежнему не считается идентификацией физического
  объекта.

Проверенный portable-артефакт:

```text
ALGA_VECTOR-1.0.0rc2-Windows-x64-onedir.zip
```

SHA-256:

```text
C4B1024D7A1AB8D2CBE590004F8C90DE0C2A4BB735360A0A3D3AC7AF73C835AE
```

Release gate: Ruff PASS, strict Mypy PASS (115 файлов), Pytest PASS
(559 тестов), source/frozen Live/Safe/Demo smoke PASS, portable extract and
Safe smoke PASS.

Документы:

- [`docs/RELEASE_100RC2_RU.md`](docs/RELEASE_100RC2_RU.md)
- [`docs/BUILD_REPORT_100RC2_RU.md`](docs/BUILD_REPORT_100RC2_RU.md)
- [`docs/FIELD_DEBUG_RF_PIPELINE_RU.md`](docs/FIELD_DEBUG_RF_PIPELINE_RU.md)
- [`docs/ALGA_VECTOR_100_PRODUCT_ARCHITECTURE_RU.md`](docs/ALGA_VECTOR_100_PRODUCT_ARCHITECTURE_RU.md)

## 1.0.0rc1 — target-centric operator platform

Дата кандидата: 27 июля 2026 года.

Главное:

- `NormalizedEvent` проецируется в bounded `FusedTarget`;
- exact/semantic dedup не допускает повторного создания одной цели;
- корреляция не выполняется только по близости времени;
- lifecycle цели: `active`, `holding`, `stale`, `tombstoned`;
- словесные стадии подтверждения заменяют проценты в SIMPLE MODE;
- SIMPLE MODE стал экраном решения: hero status, цель, сектор, рекомендация,
  до пяти важных событий и семь статусов готовности;
- EXPERT MODE получил отдельную страницу «Цели»;
- runtime snapshot публикует `targets`, `current_target` и `sensor_readiness`;
- config обновлён до `schema_version: 6`;
- направление отображается только из свежего валидированного внешнего DF;
- дальность, координаты и физическая идентичность не выводятся из RF-уровня.

Проверенный локальный portable-артефакт:

```text
ALGA_VECTOR-1.0.0rc1-Windows-x64-onedir.zip
```

SHA-256 локального release-gate артефакта:

```text
C8316F4843F01D4481BEC31EEFCDAFD6191C6B8CC2B58B00C95FEE8AB3A5D2A4
```

Документы:

- [`docs/RELEASE_100RC1_RU.md`](docs/RELEASE_100RC1_RU.md)
- [`docs/BUILD_REPORT_100RC1_RU.md`](docs/BUILD_REPORT_100RC1_RU.md)
- [`docs/ALGA_VECTOR_100_PRODUCT_ARCHITECTURE_RU.md`](docs/ALGA_VECTOR_100_PRODUCT_ARCHITECTURE_RU.md)

## 0.7.0 — SIMPLE MODE / EXPERT MODE

Дата evidence: 26 июля 2026 года.

Главное:

- один runtime обслуживает два интерфейса без перезапуска acquisition;
- добавлена стартовая вкладка «Простая обстановка»;
- технические страницы сохранены в EXPERT MODE;
- добавлен `signal_processor/` с versioned normalized schema;
- bounded event bus выполняет dedup, приоритизацию и isolation подписчиков;
- human-readable interpreter формирует краткое объяснение и рекомендацию;
- отсутствующий сенсор превращается в видимое `SENSOR_UNAVAILABLE`;
- глобальная плашка и SIMPLE MODE используют один normalized primary event;
- направление допускается только из свежего внешнего валидированного DF;
- identity-like события закрыты fail-closed policy gate;
- config остаётся на `schema_version: 5`, старые `guided`/`expert` профили
  совместимы.

Release gate:

- Ruff: PASS;
- strict Mypy: PASS, 104 исходных файла;
- Pytest: PASS, 460 тестов;
- source/frozen Live, Safe и Demo smoke: PASS;
- portable extract + preflight + Safe smoke: PASS;
- визуальный QA SIMPLE/EXPERT 1440×900: PASS.

Артефакт:

```text
ALGA_VECTOR-0.7.0-Windows-x64-onedir.zip
SHA-256:
B40CF7C9CC6A0FBF9556888265D791DD8566C5CF4FDF7C7A7916BFA2E2A5316E
```

Документы:

- [`docs/RELEASE_070_RU.md`](docs/RELEASE_070_RU.md)
- [`docs/BUILD_REPORT_070_RU.md`](docs/BUILD_REPORT_070_RU.md)
- [`docs/SIMPLE_EXPERT_ARCHITECTURE_RU.md`](docs/SIMPLE_EXPERT_ARCHITECTURE_RU.md)

## 0.6.1 — автообзор и объяснимые RF-события

Дата evidence: 26 июля 2026 года.

Главное:

- capability-gated последовательный обзор VHF/UHF/L/S/C и подтверждённого
  диапазона выбранного приёмника;
- каждый участок имеет собственные baseline и temporal state;
- первый кадр после retune считается warm-up;
- источник плана закреплён за одним приёмником;
- уведомление формируется только для alertable `confirmed`/`holding`;
- категории описывают общую форму RF-наблюдения: voice-like, packet-like,
  carrier, narrowband/broadband burst, interference/noise или unknown;
- отображается тренд принятого уровня без ложного преобразования в километры;
- внешний bearing проходит freshness/calibration/evidence gate.

Ограничение: один приёмник просматривает окна последовательно и может
пропустить короткий эпизод. RF-категория не является идентификацией объекта.

Release gate: Ruff PASS, strict Mypy 95 файлов, 431 тест, source/frozen
smoke и portable smoke PASS.

Артефакт:

```text
ALGA_VECTOR-0.6.1-Windows-x64-onedir.zip
SHA-256:
959EA2A6B0553475AFF6DB1EA1E304EA857D27996701A4E181CAE70FBF8AE26A
```

Документы:

- [`docs/RELEASE_061_RU.md`](docs/RELEASE_061_RU.md)
- [`docs/BUILD_REPORT_061_RU.md`](docs/BUILD_REPORT_061_RU.md)

## 0.6.0 — запускаемый мультисенсорный foundation

Дата evidence: 26 июля 2026 года.

Главное:

- PySide6 shell, dashboard, devices page и onboarding;
- явные Live, Safe и Demo;
- deterministic fake device adapters;
- structured JSONL logs и локальная диагностика;
- acoustic PCM feature/detection core;
- локальный гражданский ADS-B parser для `aircraft.json`;
- temporal multi-sensor fusion с abstention, hysteresis и evidence;
- config обновлён до `schema_version: 5`;
- PyInstaller GUI/CLI, portable ZIP и Inno Setup skeleton.

Bundled live microphone capture, управление dump1090 и конкретный внешний
DF-adapter в этот релиз не входили.

Release gate: Ruff PASS, strict Mypy 92 файла, 368 тестов, source/frozen
smoke и portable extraction smoke PASS.

Артефакт:

```text
ALGA_VECTOR-0.6.0-Windows-x64-onedir.zip
SHA-256:
861DB6FB57F84D23A151A7B964C8442BAC7BD1EC9D4D48ECD4BBAC4D0D6CE3B5
```

Документы:

- [`docs/RELEASE_060_RU.md`](docs/RELEASE_060_RU.md)
- [`docs/BUILD_REPORT_060_RU.md`](docs/BUILD_REPORT_060_RU.md)
- [`docs/ALGA_VECTOR_060_FOUNDATION_RU.md`](docs/ALGA_VECTOR_060_FOUNDATION_RU.md)

## 0.5.0 — production-oriented receive-only RF

Дата evidence: 26 июля 2026 года.

Главное:

- основной workflow очищен до
  `receiver → measured frame → quality gate → temporal decision → RF event`;
- Direction вынесен в отдельный optional-контур;
- без валидного bearing система показывает `unavailable`;
- RF-классы ограничены общими формами сигнала, включая `unknown`;
- temporal подтверждение по умолчанию требует нескольких кадров и dwell;
- добавлены HackRF One/PortaPack receive-only contracts;
- добавлены capability profiles tinySA Basic/Ultra/Ultra+;
- сохранены RTL-SDR profiles `auto`, `generic`, `blog_v4`,
  `blog_v3_direct_q`;
- discovery отделён от открытия устройства;
- Guided и Expert используют одну измерительную математику;
- карта/GPS были скрыты из стандартного workflow, legacy-поля сохранены для
  совместимости schema v4.

Release gate: 298 тестов, Ruff PASS, strict Mypy 78 файлов, source/frozen
smoke PASS. Физическая матрица устройств в этом прогоне не выполнялась.

Артефакт:

```text
ALGA_VECTOR-0.5.0-Windows-x64-onedir.zip
SHA-256:
135D1E15488B9B9AD982C811F5474CA1369EC161A7A7E827B6E723CA704E4B44
```

Документы:

- [`docs/RELEASE_050_RU.md`](docs/RELEASE_050_RU.md)
- [`docs/ALGA_CIVIL_RF_050_ARCHITECTURE_RU.md`](docs/ALGA_CIVIL_RF_050_ARCHITECTURE_RU.md)

## 0.4.1 — hardening RF/SDR-контура

Дата evidence: 26 июля 2026 года.

Версия закрыла воспроизведённый дефект, при котором один FFT-bin в одном кадре
мог немедленно выглядеть как достоверное RF-событие.

Главное:

- покадровый анализ отделён от окончательного temporal decision;
- добавлены temporal FSM, hysteresis, debounce и release hold;
- один всплеск больше не подтверждает устойчивый эпизод;
- persistence привязан к сопровождаемому спектральному компоненту;
- качество входа отделено от эвристической силы признаков;
- неожиданные acquisition exceptions становятся видимыми incidents;
- повреждённый device handle закрывается и не переиспользуется;
- RTL-SDR retune получает bounded settling discard;
- PSD строится robust Welch-оценкой;
- confidence явно не считается калиброванной вероятностью.

Release gate: 249/249 тестов, Ruff PASS, strict Mypy 69 файлов,
source/frozen/portable smoke PASS.

Артефакт:

```text
ALGA_VECTOR-0.4.1-Windows-x64-onedir.zip
SHA-256:
B79442F5E17FAF7C781601581B8CE299DF542CBE6C657FB16B697594A3ABE808
```

Документ:

- [`docs/PRODUCTION_AUDIT_041_RU.md`](docs/PRODUCTION_AUDIT_041_RU.md)

## 0.4.0 — Windows engineering build

Дата evidence: 25 июля 2026 года.

Главное:

- рабочие страницы dashboard, devices, spectrum, events, map, diagnostics и
  settings;
- Live стал default, Safe отключал реальные приёмники, Demo использовал
  маркированные simulated-источники;
- config `schema_version: 4` с миграциями v1/v2/v3;
- real-adapter worker отделён от GUI;
- bounded descriptor-only RTL-SDR discovery;
- tinySA и RTL-SDR adapters с явным connection;
- spectrum, passive RF events и processed-spectrum recording;
- bounded online map cache, raster MBTiles, база и GPS/NMEA;
- Guided и Expert;
- redacted support bundle.

Release gate: Ruff PASS, strict Mypy 68 файлов, 217/217 тестов,
descriptor preflight и source/frozen smoke PASS. Проверенный ранее RTL-SDR
Blog V4 отдал IQ/spectrum, но это не являлось полной аппаратной матрицей.

Артефакт:

```text
ALGA_VECTOR-0.4.0-Windows-x64-onedir.zip
SHA-256:
049FDBC19C4E49F450551EB881CDA79C0FA6A81756A3B331599C26FEFD1ECDF5
```

Исторические документы:

- [`docs/VERIFICATION_REPORT.md`](docs/VERIFICATION_REPORT.md)
- [`docs/PRODUCTION_STATUS.md`](docs/PRODUCTION_STATUS.md)

## 0.3.0 — ранний операторский workflow

Дата артефакта: 25 июля 2026 года.

По найденному `README_FIRST_RU.txt` и PE metadata подтверждаются:

- GUI и CLI version `0.3.0`;
- обычный запуск в Live;
- явный `--demo`;
- discovery и явное включение RTL-SDR;
- измеренный фон и общие типы радиоизменения;
- карта, локальный bounded cache, raster MBTiles и ручная геометрия от базы;
- режимы «Новичок» и «Эксперт» на одном runtime;
- явное ограничение: один RTL-SDR не определяет тип аппарата, расстояние,
  азимут или приближение.

Артефакт:

```text
ALGA_VECTOR-0.3.0-Windows-x64-onedir.zip
SHA-256:
8A99086E25D88848D379C25A66F0B3E3C52B558939158E7C5E46BCCCC3C32235
```

## 0.2.1 — исторический запускаемый build

Дата распакованного артефакта: 25 июля 2026 года.  
Дата упаковки сохранённого legacy-каталога: 26 июля 2026 года.

В дереве найдены распакованные:

```text
ALGA VECTOR.exe
ALGA VECTOR CLI.exe
```

PE FileVersion/ProductVersion обоих файлов — `0.2.1`. Сохранённый onedir
каталог упакован без изменения содержимого в отдельный legacy ZIP:

```text
ALGA_VECTOR-0.2.1-Windows-x64-onedir.zip
SHA-256:
964CB42930851558B7B283C0465F16C9C82F317C0713154D644A9419F8D090D4
```

Исходный source commit и отдельный release report этой версии не сохранились,
поэтому возможности 0.2.1 здесь не реконструируются. GitHub source archive для
этой версии публиковаться не должен: ZIP является только историческим
исполняемым артефактом. Версия не рекомендуется к эксплуатации.

## Как публикуется историческая линейка

В доступном workspace нет исходных git commits для версий 0.2.1–0.6.1.
Поэтому старые ZIP публикуются в одном GitHub Release
`Legacy Windows binaries 0.2.1–0.6.1` с явной пометкой
`source snapshot unavailable`.

Теги `v0.2.1`–`v0.6.1` не создаются на маркерном или более новом коммите
исходников: автоматически сгенерированные GitHub source archives содержали бы
исходники другой версии. Версионный тег `v0.7.0` указывает на настоящий
опубликованный source snapshot 0.7.0.

## Общие правила выбора версии

- Для новой установки используйте только актуальный release.
- Не переносите весь пользовательский каталог старой версии без резервной
  копии и проверки миграции.
- Demo не является аппаратным acceptance test.
- Сравнивайте SHA-256 скачанного ZIP с checksum конкретного release.
- Не запускайте одновременно две версии с одним SDR/COM-портом.
- Старые версии не получают исправления безопасности, если это отдельно не
  объявлено.
- Release notes описывают программный gate, а не полевую точность конкретной
  антенны, приёмника и радиосреды.
