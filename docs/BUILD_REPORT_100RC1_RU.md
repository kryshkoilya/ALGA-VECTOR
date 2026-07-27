# ALGA VECTOR 1.0.0rc1 — отчёт сборки

Дата проверки: 2026-07-27
Целевая платформа: Windows 11 x64, AMD64
Результат локального release gate: **PASS**

## Проверенный артефакт

- файл: `dist/ALGA_VECTOR-1.0.0rc1-Windows-x64-onedir.zip`;
- размер: 64 781 694 байта, около 61,78 MiB;
- SHA-256:
  `C8316F4843F01D4481BEC31EEFCDAFD6191C6B8CC2B58B00C95FEE8AB3A5D2A4`;
- checksum-файл:
  `dist/ALGA_VECTOR-1.0.0rc1-Windows-x64-onedir.zip.sha256.txt`;
- элементов в ZIP: 292;
- GUI FileVersion/ProductVersion: `1.0.0rc1`;
- CLI FileVersion/ProductVersion: `1.0.0rc1`;
- numeric PE fixed version: `1.0.0.0`;
- Authenticode GUI/CLI: `NotSigned`.

Checksum пересчитан независимо после сборки и совпал с checksum-файлом.
В архиве проверено наличие:

- `ALGA VECTOR.exe`;
- `ALGA VECTOR CLI.exe`;
- `README_FIRST_RU.txt`;
- `RELEASE_NOTES_RU.md`;
- `THIRD_PARTY_NOTICES.md`.

Portable-кандидат был опубликован в `dist` только после временной распаковки,
hardware preflight и Safe headless smoke из распакованной копии.

Сборка выполнена с `-SkipInstaller`: Inno Setup 6 в этой среде не подтверждён.
Проверенный артефакт данного отчёта — portable onedir ZIP.

## Автоматические проверки

| Проверка | Результат |
|---|---|
| Ruff | PASS |
| strict Mypy | PASS, 114 source-файлов |
| pytest | PASS, 534 теста |
| source hardware preflight | PASS |
| source default Live smoke | PASS |
| source explicit Live smoke | PASS |
| source Safe smoke | PASS |
| source Demo smoke | PASS |
| frozen CLI hardware preflight | PASS |
| frozen CLI default Live / explicit Live / Safe / Demo smoke | PASS |
| frozen GUI default Live / Safe headless smoke | PASS |
| portable candidate extract + CLI preflight + Safe smoke | PASS |
| GUI/CLI PE version resources | PASS |
| состав portable ZIP | PASS |
| SHA-256 portable ZIP | PASS |
| SIMPLE MODE render 1440×900 | PASS, визуально проверен |
| EXPERT MODE «Цели» render 1440×900 | PASS, визуально проверен |

Headless smoke с отдельным `--data-dir` использует изолированный test mutex.
Обычный запуск по-прежнему защищён единым per-user mutex. Это позволило
проверить сборку, не закрывая уже работающие пользовательские экземпляры и не
разделяя с ними журналы или конфигурацию.

## Проверенные fail-closed сценарии

- один импульс или одна частота не создают подтверждённую цель;
- истёкшая `TARGET_CONFIRMED` теряет confirmed-stage и оперативное действие;
- `HOLDING`, `STALE`, tombstone и invalid duck-typed target не становятся
  текущей целью;
- delayed expired event не создаёт кратковременную `ACTIVE`-цель;
- `HOLDING` не занимает active admission capacity;
- conflicting radio/video hypotheses переходят в unknown/conflict;
- направление ниже quality gate скрывается;
- `source_id` направления должен быть атрибутирован как
  `SensorKind.DIRECTION_FINDER`;
- направление требует точного общего `episode_id` с primary observation;
- standalone или cross-episode direction остаётся контекстом;
- future, malformed, timezone-naive и expired timestamps закрываются
  fail-closed;
- фильтр `important_only` меняет ленту, но не первичную обстановку;
- таблица целей, header, badges, banner, направление и действие используют
  единый freshness verdict;
- historical-запись не показывает старый confirmed badge или safety-action.

## Аппаратный preflight

Подтверждено наличие обязательных runtime-модулей:

- `pyserial`;
- `pyrtlsdr`;
- `pyusb`.

Во время финальной проверки:

- RTL-SDR descriptor не найден;
- `hackrf_info` и `hackrf_transfer` отсутствовали;
- metadata-кандидаты tinySA не найдены;
- внешний KrakenSDR/DF не подключался;
- bundled live microphone capture не проверялся;
- физическая ADS-B сеть и passive radar не проверялись.

Это подтверждает исправность software preflight и graceful degradation, но не
является полевой проверкой чувствительности, калибровки, антенны, динамического
диапазона, вероятности обнаружения или false-alarm rate.

## Среда сборки

- Windows `10.0.26200`, определяется как Windows 11;
- Python `3.12.10`, AMD64;
- PyInstaller `6.21.0`;
- PySide6 `6.11.1`;
- NumPy `2.5.1`;
- Pydantic `2.13.4`;
- PyYAML `6.0.3`;
- platformdirs `4.11.0`;
- pyserial `3.5`;
- pyrtlsdr `0.5.0`;
- pyusb `1.3.1`;
- pytest `8.4.2`;
- Ruff `0.16.0`;
- Mypy `1.20.2`.

## Граница результата

Release gate подтверждает запускаемость, packaging, software contracts,
fail-closed поведение и визуальную целостность проверенных экранов. Он не
подтверждает:

- физический класс, модель, принадлежность, государство, намерение или IFF по
  одной RF-частоте;
- дальность по RSSI;
- координаты по одному bearing;
- качество на неизвестной аппаратной связке;
- полевую accuracy без размеченного validation dataset;
- готовность к эксплуатации без site-specific hardware/soak acceptance.

До аппаратной и полевой приёмки `1.0.0rc1` остаётся предварительной версией;
стабильной рекомендуемой версией проекта остаётся `0.7.0`.

GitHub Release для `v1.0.0rc1` намеренно не создаётся из dirty tree или
непроверенного тега. Release workflow допускается только после merge, зелёного
CI и тега, указывающего на точный проверенный commit.
