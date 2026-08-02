# ALGA VECTOR 1.0.0rc1 — target-centric operator platform

Дата кандидата: 27 июля 2026 года.

`1.0.0rc1` — предварительный выпуск перед стабильной 1.0. Он переводит
ALGA VECTOR из интерфейса, ориентированного на отдельные события, в
операторскую платформу:

```text
измерения → normalized events → fused targets → operator presentation
```

Стабильной рекомендуемой версией до завершения аппаратной приёмки остаётся
`0.7.0`. RC предназначен для проверки новой модели, интерфейса и packaging
на конкретной Windows-конфигурации.

> Разработал: Буйвол и Задира

## Главное

- несколько совместимых наблюдений объединяются в одну `FusedTarget`;
- временная близость сама по себе не объединяет события;
- введены lifecycle `active / holding / stale / tombstoned`;
- SIMPLE MODE показывает словесные стадии:
  `Фон`, `Подозрительная активность`, `Вероятный источник`,
  `Вероятная цель`, `Подтверждённая цель`;
- проценты и техническая сила evidence остаются только в EXPERT MODE;
- SIMPLE MODE содержит hero status, текущую цель, компактный сектор,
  следующий шаг, до пяти важных событий и готовность семи сенсорных ролей;
- EXPERT MODE получил страницу «Цели» с attribution, evidence, временем,
  ограничениями и подробной рекомендацией;
- config обновлён до `schema_version: 6` и мигрирует старые профили;
- runtime snapshot теперь публикует `targets`, `current_target` и
  `sensor_readiness`.

## Target aggregator

Новый пакет `alga_vector.targets` реализует:

- exact и semantic deduplication;
- обнаружение конфликта одного immutable `event_id` с разным содержимым;
- fail-closed correlation gates по явной связи observation/episode,
  совместимому измерению или валидному fusion bridge;
- bounded memory и лимит активных целей;
- exponential time decay как эвристическую силу, а не вероятность;
- holding, stale, retirement и ограниченное хранение tombstone;
- сохранение вклада каждого источника без суммирования повторов одного
  сенсора как независимых подтверждений;
- только явно переданный свежий `DirectionEstimate` или `ValidatedZone`.

## Fail-closed hardening

Финальный RC gate дополнительно закрывает отрицательные сценарии:

- просроченная `TARGET_CONFIRMED` не остаётся подтверждённой или actionable;
- `HOLDING`, `STALE` и historical-записи не выбираются как текущая цель;
- слабый, неатрибутированный или связанный с другим episode пеленг скрывается;
- направление требует свежий внешний DF, DF-attribution и явную связь с целью;
- отдельный `DIRECTION_ESTIMATED` остаётся контекстом и не создаёт цель;
- `HOLDING` не блокирует admission новой активной цели;
- conflicting radio/video hypotheses одного уровня переходят в unknown/conflict;
- `important_only` фильтрует ленту, но не изменяет первичную обстановку;
- таблица, badges, banner, направление и действие используют один freshness
  verdict.

## Sensor readiness

Оператор всегда видит семь стабильных ролей:

1. TinySA;
2. RTL-SDR;
3. KrakenSDR;
4. Acoustic;
5. ADS-B;
6. Passive radar;
7. Fusion.

Каждая роль имеет состояние `ready / limited / unavailable`, короткую причину
и влияние отсутствия сенсора. Отсутствующий KrakenSDR не заменяется
синтетическим лучом: интерфейс пишет, что направление не определяется.

## Human-readable contract

События и цели имеют три уровня текста:

- `technical_label`;
- `operator_label`;
- `operator_explanation`.

Рекомендация разделена на короткий следующий шаг и подробное объяснение.
SIMPLE MODE не читает raw IQ, waterfall или внутренние SDR-метрики напрямую.

## Граница достоверности

RC не заявляет:

- определение дальности по RSSI;
- координаты или позицию источника по одному азимуту;
- физическую идентичность, модель, принадлежность, государство, намерение,
  IFF или «свой/чужой»;
- подтверждение объекта по одному RF-импульсу;
- направление без свежего валидированного внешнего DF;
- полноту ADS-B-контекста;
- передачу, подавление или модификацию радиосигнала.

`LIKELY_VIDEO_LINK` означает совместимость наблюдаемой формы с общим
видеоподобным каналом, а не доказанный тип физического объекта.
`LIKELY_DRONE_SIGNATURE` и `TARGET_CONFIRMED` проходят отдельный fail-closed
policy gate. В дополнение к `ValidatedIdentityEvidence` первая стадия требует
минимум одно, а вторая — минимум два свежих, явно атрибутированных независимых
non-RF физических подтверждения.

## Файлы реализации

- `src/alga_vector/targets/aggregator.py`
- `src/alga_vector/targets/dedup.py`
- `src/alga_vector/targets/models.py`
- `src/alga_vector/targets/readiness.py`
- `src/alga_vector/targets/recommendations.py`
- `src/alga_vector/signal_processor/processor.py`
- `src/alga_vector/ui/pages/simple_situation.py`
- `src/alga_vector/ui/pages/targets.py`
- `src/alga_vector/ui/widgets/target_card.py`
- `src/alga_vector/ui/widgets/sector_view.py`
- `src/alga_vector/ui/widgets/sensor_readiness.py`

Полное описание решений A–H:
[`ALGA_VECTOR_100_PRODUCT_ARCHITECTURE_RU.md`](ALGA_VECTOR_100_PRODUCT_ARCHITECTURE_RU.md).

## Проверка

Перед использованием на объекте:

1. проверьте SHA-256 portable ZIP по соседнему release asset;
2. запустите `ALGA VECTOR CLI.exe --hardware-preflight`;
3. выполните Safe smoke без оборудования;
4. проверьте Live на точной связке Windows, драйвера, firmware, USB,
   антенны, фильтров и аттенюатора;
5. отдельно проверьте timestamp, calibration id и freshness внешнего DF;
6. зафиксируйте acceptance criteria и сохраните support bundle.

Автоматические тесты подтверждают программные контракты, но не заменяют
полевую калибровку и аппаратную приёмку.

Локальный финальный gate перед упаковкой: Ruff PASS, strict Mypy PASS по
114 source-файлам, pytest PASS — 534 теста, SIMPLE/EXPERT render 1440×900
визуально проверен.

## Известные ограничения RC

- Windows EXE не имеют Authenticode-подписи;
- локальная сборка поставляется как portable onedir ZIP; installer публикуется
  только после отдельного проверенного Inno Setup gate;
- bundled live microphone capture и управление dump1090 не входят в RC;
- конкретный KrakenSDR adapter не bundled: используется внешний
  валидированный ingestion contract;
- passive radar ограничен разрешёнными записанными или лабораторными входами;
- release workflow проходит первый end-to-end RC-прогон и поэтому сам является
  частью приёмки.
