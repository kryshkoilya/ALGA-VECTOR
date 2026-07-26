# ALGA VECTOR 0.7.0 — SIMPLE MODE / EXPERT MODE

Дата: 2026-07-26  
Разработал: Буйвол и Задира

## Главное

Версия 0.7.0 превращает технический набор экранов в двухрежимную операторскую
платформу. Один и тот же backend теперь формирует единый нормализованный поток
событий и два представления:

- `SIMPLE MODE` — понятная обстановка, подтверждение, сектор и следующий шаг;
- `EXPERT MODE` — спектр, события, пеленгация, карта и диагностика.

Переключение режима не меняет acquisition, пороги, temporal state или
measurement math. Старое значение профиля `guided` совместимо с SIMPLE MODE,
`expert` — с EXPERT MODE.

## Новый signal_processor

Добавлен пакет `src/alga_vector/signal_processor/`:

- versioned `NormalizedEvent`;
- `UnifiedEventBus` с bounded history, dedup и isolation ошибок подписчиков;
- `SnapshotEventNormalizer`;
- `FailClosedEventPolicy`;
- `RecommendationEngine`;
- `HumanReadableInterpreter`;
- `UnifiedSignalProcessor`.

Runtime публикует в каждом `SystemSnapshot`:

- `operator_situation`;
- `normalized_events`.

Дополнительные адаптеры могут передавать уже нормализованные наблюдения через
`ApplicationRuntime.ingest_normalized_event()`. Этот вход проходит ту же
policy-проверку, что и штатные источники.

## События

Поддержаны:

- `NOISE_BACKGROUND`;
- `RADIO_ACTIVITY_DETECTED`;
- `LIKELY_HANDHELD_RADIO`;
- `LIKELY_VIDEO_LINK`;
- `LIKELY_DRONE_SIGNATURE`;
- `ADSB_CONTACT`;
- `ACOUSTIC_ANOMALY`;
- `DIRECTION_ESTIMATED`;
- `MULTISENSOR_CORRELATED`;
- `TARGET_CONFIRMED`;
- `SENSOR_UNAVAILABLE`.

Наличие enum не означает автоматическую выдачу такого решения. Identity-like
события закрыты policy gate.

## Границы достоверности

- Частота, диапазон или RSSI сами по себе не создают `LIKELY_DRONE_SIGNATURE`
  или `TARGET_CONFIRMED`.
- `LIKELY_HANDHELD_RADIO` и `LIKELY_VIDEO_LINK` требуют отдельного
  валидированного классификатора; обычный RF pipeline выдаёт общий
  `RADIO_ACTIVITY_DETECTED`.
- Generic RF+acoustic fusion создаёт `MULTISENSOR_CORRELATED`, а не
  `TARGET_CONFIRMED`.
- `TARGET_CONFIRMED` требует валидированного identity record и минимум двух
  явно атрибутированных независимых подтверждений.
- Азимут попадает в operator situation только из свежего внешнего DF с валидной
  калибровкой.
- RSSI не преобразуется в дальность; один bearing не преобразуется в
  координаты.
- Confidence — эвристическая сила признаков, не калиброванная вероятность.
- ADS-B остаётся гражданским cooperative context, не IFF.

## SIMPLE MODE

Новая вкладка `Простая обстановка` показывает:

- один крупный режим: тишина / фон / активность / подтверждённая цель;
- короткое объяснение;
- свежий валидный сектор либо точную причину недоступности;
- словесную силу подтверждения;
- одно рекомендуемое действие;
- последние события простым языком;
- фильтр `Показывать только важное`;
- состояние отсутствующих сенсоров.

Экран читает только `snapshot.operator_situation`. Он намеренно не строит
заключение из raw spectrum, IQ, RSSI или legacy fields.

## EXPERT MODE

Сохранены:

- Dashboard;
- Devices;
- Spectrum / waterfall;
- Signal Events;
- Direction;
- Map;
- Diagnostics;
- Settings.

Карта доступна в экспертной навигации и сохраняет прежние ограничения: база и
картографические инструменты не являются позицией RF-источника.

## Ошибки и диагностика

Отказ `signal_processor`:

- не роняет runtime;
- не подменяется сырыми данными;
- создаёт видимый incident `SIGNAL_PROCESSOR.FAILED`;
- снижает readiness;
- записывается в structured JSONL log.

Смена основной operator situation также записывается структурированным
событием. Глобальная плашка использует тот же normalized primary event, что и
SIMPLE MODE.

## Совместимость и миграция

Переписывать detector, RF decision engine, acoustic core, direction service,
ADS-B reader, fusion, storage и hardware adapters не потребовалось. Новый слой
установлен между их стабильными результатами и UI.

`schema_version` профиля остаётся 5. Существующие `guided` / `expert` профили
продолжают загружаться.

## Release hardening

- reconnect изолированного hardware worker получил единый end-to-end budget:
  stop, spawn, handshake, RPC и cleanup больше не складывают независимые полные
  таймауты;
- при неудачном восстановлении сохраняется исходный fail-closed reason code, а
  вторичная причина записывается в `technical_details`;
- CI и сборка используют проверенный `requirements-lock.txt`, build backend и
  GitHub Actions закреплены версиями/SHA;
- portable-пакет включает `THIRD_PARTY_NOTICES.md`;
- GitHub release workflow запускается вручную для существующего тега и
  отказывается молча перезаписывать опубликованный бинарный артефакт.

## Проверка

Release gate включает:

- Ruff;
- strict Mypy;
- полный pytest;
- hardware preflight;
- source live/safe/demo headless smoke;
- frozen CLI live/safe/demo smoke;
- frozen GUI smoke;
- проверку версии PE-файлов;
- распаковку и повторную smoke-проверку portable ZIP.

Физическая точность, чувствительность и false-positive rate конкретной
инсталляции требуют отдельного acceptance test с реальными антеннами,
приёмниками, калибровкой и размеченными записями. Автоматические тесты не
заменяют полевую валидацию.
