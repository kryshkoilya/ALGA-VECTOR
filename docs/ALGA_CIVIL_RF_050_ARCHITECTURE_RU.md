# ALGA VECTOR v0.5 — архитектура гражданского RF-мониторинга

Статус документа: реализованный архитектурный контракт v0.5.0 и оставшиеся
release gates.

Область применения: законный пассивный приём и лабораторный анализ
радиочастотного спектра. Продукт не идентифицирует физические объекты по одному
RF-наблюдению, не определяет координаты и не вычисляет направление без отдельного
валидированного источника пеленга.

Подпись продукта: **«Разработал: Буйвол и Задира»**.

## A. Варианты названия

1. **ALGA VECTOR** — рекомендуемое имя; сохраняет узнаваемость продукта и хорошо
   подходит для экрана направления.
2. **ALGA SPECTRUM** — прямо описывает основную измерительную область.
3. **ALGA OBSERVER** — подчёркивает пассивное наблюдение.
4. **ALGA SIGNAL** — нейтральное имя для классификации формы сигналов.
5. **ALGA TRACE** — акцент на журнале и временной истории эпизодов.
6. **ALGA BEARING** — подходит для версии с внешним валидированным DF-сенсором.
7. **ALGA LAB** — лабораторная и учебная направленность.
8. **ALGA SCOPE** — краткое имя для спектрального инструмента.
9. **ALGA MONITOR** — понятное назначение без обещания идентификации.
10. **ALGA INSIGHT** — акцент на объяснимости и диагностике.

Решение: оставить **ALGA VECTOR**. Смена имени не даёт продуктовой пользы,
а слово VECTOR корректно описывает реализованный policy-gated вход внешнего
датчика направления, не превращая его в неподтверждённую геолокацию.

## B. Целевая архитектура

### 1. Composition и configuration

- `bootstrap.py` создаёт процесс в одном из режимов: `live`, `demo`, `safe`.
- Pydantic-конфигурация валидирует весь профиль до запуска устройства.
- Активная схема намеренно остаётся `schema_version: 4`: новые поля адаптеров
  имеют безопасные defaults и не требуют несовместимой миграции.
- Диапазон частот, полоса и частота дискретизации проверяются по capability
  выбранного устройства, а не по одному глобальному диапазону UI.
- Старые поля `map/location` читаются для совместимости, но скрыты и не
  участвуют в навигации, onboarding или Settings payload v0.5.0. Без видимой
  страницы тайлы не запрашиваются.
- Demo никогда не сохраняется поверх live-профиля.

### 2. Device layer

Discovery вынесен в отдельные bounded services и не смешан с работающим
адаптером. `DeviceAdapter` предоставляет inspect/read spectrum/reconnect/close,
а process manager отвечает за start/stop, timeout, process-liveness и IPC.
Capability поступает из `receiver_profile` и device metrics.

Поддерживаемые типы:

- **HackRF One / PortaPack в HackRF USB mode**:
  - приём только;
  - обнаружение через официальный `hackrf_info`;
  - захват signed 8-bit I/Q через официальный `hackrf_transfer`;
  - частоты 1 МГц–6 ГГц;
  - 2–20 MS/s;
  - приложение не содержит команды передачи, питания антенны или включения
    выходного усилителя;
  - PortaPack не обещается как доступный, пока устройство не переведено в
    стандартный HackRF USB mode.
- **tinySA Ultra / Ultra+**:
  - обнаружение только по метаданным системных serial-port descriptors;
  - произвольные COM-порты не открываются автоматически;
  - модель и firmware читаются только после явного добавления выбранного COM;
  - capability выбирается по прочитанной identity модели
    ZS405/ZS406/ZS407 либо по явному operator override;
  - Ultra mode требует отдельного подтверждения оператора и не включается
    приложением автоматически;
  - Ultra/harmonic режимы показываются как режимы с измерительными
    ограничениями, а не как эквивалент полноценного real-time SDR; harmonic
    mode не входит в поддерживаемый профиль.
- **RTL-SDR**:
  - сохраняется существующая поддержка и профили аппаратных ревизий;
  - диапазон зависит от подтверждённого профиля, включая отдельное явное
    подтверждение direct-sampling.
- **External direction sensor**:
  - отдельный интерфейс, не смешанный с RF-приёмником;
  - вход принимается только с timestamp, calibration id, quality и uncertainty.
  - bundled adapter конкретной модели внешнего DF-сенсора в v0.5.0 отсутствует.

Все включённые real adapters отделены от GUI одним disposable
worker-процессом; это изоляция процесса, а не отдельный worker на каждое
устройство. Ошибка DLL, USB, драйвера или worker не должна завершать GUI.

### 3. Acquisition layer

- Фоновый acquisition отделён от UI timer; worker принимает не более одной
  незавершённой команды, а runtime публикует последний валидный кадр.
- Один и тот же `source_id`/`sequence`/`captured_at` не анализируется повторно.
- Монотонные timestamps и отдельное UTC-время для журнала.
- Timeout на discovery, probe, start, read и stop.
- Сбой, timeout или завершение worker переводят источник в fail-closed
  состояние с причиной; явный reconnect перезапускает worker.
- Автоматические quarantine и retry/backoff с jitter в v0.5.0 не заявляются.
- Для HackRF `hackrf_transfer -r -` возвращает signed 8-bit IQ через bounded
  stdout непосредственно в память; размер и время subprocess ограничены.
- Для tinySA известны запрошенное число sweep-точек и ограничения профиля;
  измерение маркируется как последовательный sweep. Текущая интеграция не
  выдаёт фактически измеренные sweep duration/RBW, поэтому короткие события
  между точками нельзя считать гарантированно отсутствующими.

### 4. Signal processing layer

Pipeline:

1. Проверка формы, конечности и монотонности входа.
2. Нормализация только в рамках известной шкалы источника.
3. Оценка noise floor устойчивой медианной статистикой.
4. Peak detection и connected components над порогом.
5. Оценка occupied bandwidth и peak excess.
6. Временные признаки: длительность, duty cycle, повторяемость, устойчивость
   центра и полосы.
7. Quality flags: clipping, stale frame, dropped frames, sparse sweep,
   uncalibrated level, Ultra-mode ambiguity, missing metadata.

Если уровень не калиброван в dBm, UI пишет **«относительный уровень»**. Если
измеряется превышение над локальным noise floor, UI пишет **«SNR-подобный
показатель / превышение над фоном»**, а не «точный SNR».

### 5. Decision layer

Разрешённые классы описывают форму принятого сигнала:

- `carrier`;
- `narrowband_burst`;
- `broadband_burst`;
- `packet_like`;
- `voice_like`;
- `periodic_beacon_like`;
- `interference_noise_like`;
- `unknown`.

Состояния эпизода:

```text
idle → candidate → confirmed → holding → cleared
                  ↘ rejected
```

Правила:

- один импульс не создаёт подтверждённое оповещение;
- подтверждение требует временной устойчивости или согласованной повторяемости;
- debounce подавляет повторные баннеры одного эпизода;
- hysteresis разделяет порог входа и выхода;
- качество данных может понизить confidence или полностью запретить алерт;
- confidence — эвристическая сила признаков, не вероятность истинности;
- решение хранит «за», «против», «не хватает», вклад сенсоров и ограничения;
- физический объект, назначение передатчика, расстояние и приближение не
  выводятся из RF-формы.

### 6. Direction layer

`DirectionObservation` содержит:

- `source`: unavailable / manual / external / simulated;
- `bearing_deg`;
- `uncertainty_deg`;
- `confidence`;
- `quality`;
- `captured_at`;
- `source_id`;
- `reason_code` и `message_ru`;
- для external — `ExternalDirectionEvidence` с calibration id, временем
  калибровки/evidence, sample count, quality score и validity.

Правила доверия:

- manual — справочная отметка оператора, не измерение;
- external — только свежие данные и текущая калибровка;
- simulated — только Demo и всегда с постоянной маркировкой;
- отсутствие валидного источника — `Direction unavailable`;
- уровень одного RF-приёмника не преобразуется в азимут;
- расстояние, координаты и километровые кольца отсутствуют.

### 7. Storage и observability

- SQLite-журнал RF-эпизодов и переходов lifecycle.
- JSONL structured logs с correlation id, device id и operation.
- Ротация и retention.
- Health summary агрегирует snapshots устройств, capability и incidents;
  остановка worker, устаревший кадр и ошибка хранилища становятся явными
  incident/fault состояниями.
- Queue pressure и classifier latency как отдельные метрики в v0.5.0 не
  экспортируются.
- Support bundle исключает приватные пути, содержимое raw-захватов и любые
  устаревшие координатные данные из старого профиля.
- Известные операционные ошибки отображаются как incident/fault с действием для
  пользователя; неожиданный выход acquisition loop получает отдельный код и
  запись в журнал. Это не является абсолютной гарантией перехвата любого
  возможного исключения.

### 8. Presentation layer

- PySide6 Widgets, русская локализация.
- Guided и Expert используют одни измерения и одну decision state machine.
- Guided упрощает формулировки и показывает один следующий шаг.
- Expert раскрывает частоту, полосу, level, peak excess, quality flags,
  lifecycle, decision evidence и provenance.
- Все live/replay/demo состояния имеют постоянную маркировку источника.

## C. UI-концепция

Визуальное направление: тёмный графит, сдержанная глубина полупрозрачных
поверхностей, тонкие границы, бирюзовый для нормального состояния, янтарный для
неопределённости и красный только для реальной ошибки. Никакого неона,
декоративного радара и лишних виджетов.

### Навигация

1. **Обзор** — готовность, простой ответ «что наблюдается», следующий шаг.
2. **Устройства** — явный запуск discovery, capability, драйвер, snapshot,
   recovery.
3. **Спектр** — live-график, диапазон только в пределах выбранного оборудования.
4. **События** — temporal episodes, evidence, alternatives, limitations.
5. **Направление** — валидированный bearing или честное пустое состояние.
6. **Диагностика** — incidents, logs, health, support bundle.
7. **Настройки** — режим пользователя, устройства, пороги, хранение и честное
   описание ограничений Direction. Драйвер внешнего DF здесь не настраивается.
Учебный сценарий включается отдельным process mode `--demo`; отдельной
страницы «Обучение» в навигации нет.

### Экран «Направление»

- Слева — крупный круг 360°, основные стороны света, деления 30°.
- При валидном наблюдении — луч, сектор неопределённости и короткий trail.
- В центре без данных — «Направление недоступно» и точная причина.
- Справа — источник, режим, bearing, uncertainty, свежесть, калибровка,
  качество и ограничения.
- Ручной ввод расположен справа и всегда обозначен как неизмеренный.
- RF-эпизод не создаёт направление и не связывается с лучом автоматически.

### Оповещение

Плашка содержит:

- наблюдаемое семейство сигнала;
- центральную частоту и полосу;
- длительность;
- эвристическую силу признаков;
- качество данных;
- краткое «почему»;
- ссылку на подробный decision chain.

Примеры допустимых заголовков:

- «Подтверждён узкополосный RF-эпизод»;
- «Повторяющаяся пакетоподобная активность»;
- «Широкополосное RF-изменение»;
- «Источник не классифицирован — требуется больше данных».

## D. Фактическая ключевая структура файлов

```text
src/alga_vector/
├── application/
│   └── runtime.py
├── config/
│   ├── models.py
│   └── service.py
├── devices/
│   ├── capabilities.py
│   ├── discovery.py
│   ├── receiver_discovery.py
│   ├── host_tools.py
│   ├── hackrf.py
│   ├── live.py
│   ├── manager.py
│   └── process_manager.py
├── direction/
│   ├── models.py
│   └── service.py
├── signal_analysis/
│   ├── detector.py
│   └── decision.py
├── observability/
│   ├── health.py
│   └── jsonl.py
├── storage/
│   └── journal.py
└── ui/
    ├── main_window.py
    ├── onboarding.py
    ├── signal_notifications.py
    ├── pages/
    │   ├── dashboard.py
    │   ├── devices.py
    │   ├── spectrum.py
    │   ├── events.py
    │   ├── direction.py
    │   ├── diagnostics.py
    │   └── settings.py
    └── widgets/
        └── direction_plot.py
```

Старые `maps/`, `location/` и `ui/pages/map.py` остаются как неактивный
migration/compatibility-код. Они не входят в навигацию, onboarding, видимые
Settings и новый receive workflow. Их удаление требует отдельной миграции
schema/data и в v0.5.0 не выполняется.

## E. Статус реализации и критерии готовности

### Этап 1. Контракт безопасности и совместимость — реализован

- Разрешённые выходные RF-семейства зафиксированы.
- Object attribution, range и approach не входят в decision contract.
- `schema_version: 4` сохранена намеренно; повышения схемы нет.
- Старые map/location поля читаются, но скрыты и не применяются новым UI.
- Карта/GPS отсутствуют в navigation, onboarding и быстром запуске.

Критерий: старый профиль открывается без потери данных, но видимый v0.5.0 UI
не предлагает карту/GPS и не утверждает физическую идентичность.

### Этап 2. Capability-driven hardware — реализован в коде

- Реализован HackRF descriptor discovery через `hackrf_info`.
- Реализован receive-only IQ capture через `hackrf_transfer -r -`.
- Реализован metadata-only tinySA discovery.
- Добавлены capability profiles HackRF, tinySA и RTL-SDR.
- Config и UI проверяют всё частотное окно и sample/span limits.
- Отсутствующие tools/driver/device дают fail-closed incident, а не crash.

Ограничение: реальное оборудование в рамках этого release цикла не
подтверждалось; hardware acceptance остаётся отдельным gate.

### Этап 3. Decision pipeline — реализован

- Добавлены безопасные signal families.
- Реализованы temporal smoothing, recurrence, periodicity, hysteresis,
  release hold и debounce.
- Data quality, evidence strength и heuristic score разделены.
- Decision хранит supporting/contradicting/missing evidence, alternatives и
  acquisition limitations.
- RF-эпизоды и transitions сохраняются в SQLite/WAL journal.

Критерий покрыт автоматическими тестами: одиночный импульс не подтверждается,
повторный кадр не создаёт новый алерт, а решение объясняет причины.

### Этап 4. Direction — реализован без bundled external adapter

- Реализованы models, policy service и runtime ingestion API.
- Manual, external и simulated проходят разные trust rules.
- Добавлены 360° QWidget, uncertainty cone и bounded trail.
- Карта удалена из main navigation.
- Stale/invalid data немедленно скрывает луч.

Ограничение: конкретный драйвер внешнего DF-сенсора не поставляется. Поэтому
по умолчанию live Direction остаётся unavailable, пока интегратор явно не
передаст валидированный sample.

### Этап 5. Guided UX и diagnostics — реализован

- Onboarding: welcome → experience → storage → receiver →
  interpretation/limits → finish.
- Dashboard: receiver → measured frame → interpretation/events.
- Direction optional и не влияет на RF readiness.
- Devices поддерживает отдельные discovery tabs RTL-SDR, HackRF и tinySA.
- Settings показывает capability-driven limits и ограничения направления.
- Guided и Expert используют одну измерительную математику.

### Этап 6. Verification и Windows release — автоматизирован

`packaging/build.ps1` выполняет:

- Ruff;
- strict Mypy;
- полный Pytest;
- hardware preflight;
- default-live/live/safe/demo source smokes;
- PyInstaller onedir;
- frozen GUI/CLI smokes;
- FileVersion/ProductVersion gate;
- optional portable ZIP, extraction smoke и SHA-256;
- optional Inno Setup installer.

Source и mock/fake hardware gates не считаются физической проверкой. Отдельно
остаются реальные USB/firmware/driver acceptance, hot-unplug, soak,
clean-Windows и Authenticode.

## F. Реализационный контракт

Код v0.5.0 является инкрементальным продолжением ALGA VECTOR v0.4.1:

- сохранить рабочий журнал, retention, support bundle и process isolation;
- не дублировать уже существующие detector/FSM;
- использовать HackRF и Direction как модульные расширения;
- оставить demo отдельным процессным режимом;
- покрыть каждую новую ветку ошибочного состояния тестом;
- каждое утверждение UI о частоте, качестве и направлении должно иметь
  измеряемое поле-источник в snapshot.

## Официальные источники характеристик

Ссылки ниже подтверждают программно заданные capability limits, но не
являются свидетельством физического теста ALGA VECTOR на устройстве.

- Great Scott Gadgets, HackRF One:
  https://hackrf.readthedocs.io/en/stable/hackrf_one.html
- Great Scott Gadgets, HackRF tools:
  https://hackrf.readthedocs.io/en/latest/hackrf_tools.html
- tinySA, model comparison:
  https://www.tinysa.org/wiki/pmwiki.php?n=TinySA4.Comparison
- tinySA, Ultra-mode limitations:
  https://tinysa.org/wiki/pmwiki.php?n=TinySA4.Ultra
- tinySA, USB interface:
  https://tinysa.org/wiki/pmwiki.php?n=Main.USBInterface
