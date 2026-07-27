# Участие в разработке ALGA VECTOR

Спасибо за интерес к проекту. ALGA VECTOR развивается как гражданский,
receive-only и объяснимый инструмент пассивного наблюдения. Изменение считается
готовым только тогда, когда оно сохраняет границы достоверности, покрыто
тестами и не маскирует отсутствие аппаратного evidence.

## Допустимый scope

Приветствуются:

- улучшение стабильности Device/Acquisition;
- поддержка законных receive-only сенсоров;
- качество DSP и общая классификация формы сигнала;
- temporal smoothing, hysteresis, debounce и abstention;
- объяснимый `signal_processor`;
- SIMPLE MODE и доступность интерфейса;
- EXPERT MODE, диагностика и наблюдаемость;
- безопасная обработка acoustic/ADS-B/external DF context;
- privacy, redaction, миграции конфигурации;
- тесты, документация, packaging и воспроизводимость.

Не принимаются:

- передача, глушение, подавление или управление радиосигналами;
- обход аппаратных или регуляторных ограничений;
- скрытое включение реальных адаптеров;
- выдача симуляции за Live;
- конфликтные базы частот, национальные сигнатуры или вывод принадлежности;
- псевдолокация по одному RSSI или одному bearing;
- утверждение точного типа физического объекта по одной частоте/PSD;
- код, превращающий гражданский ADS-B-контекст в IFF.

## Архитектурные инварианты

Любой pull request обязан сохранять следующие правила:

1. SIMPLE MODE читает только интерпретированные контракты
   `operator_situation`, `current_target`/`targets` и `sensor_readiness`, а не
   raw IQ/spectrum/RSSI.
2. UI не создаёт identity-решения самостоятельно.
3. Частота, band label и RSSI не подтверждают дрон, рацию или видеоканал.
4. `confidence` без model card и calibration report не называется
   вероятностью.
5. Одиночный всплеск не создаёт подтверждённый эпизод.
6. Stale/malformed observations не участвуют в текущем решении.
7. Generic RF+acoustic correlation не равна `TARGET_CONFIRMED`.
8. Измеренный bearing принимается только от свежего внешнего DF с calibration
   id, quality, uncertainty, timestamp и evidence.
9. Manual и simulated direction всегда имеют явный provenance.
10. RSSI не преобразуется в расстояние или «приближается/удаляется».
11. ADS-B — cooperative civilian context, не IFF.
12. Отказ сенсора виден оператору и не подменяется Demo.
13. Ошибка pipeline не должна завершаться silent failure.
14. Discovery не открывает произвольные устройства и не начинает capture.
15. Проект остаётся receive-only.

## Подготовка среды

Требования:

- Windows 11 x64;
- Python 3.12 x64;
- PowerShell;
- Git;
- для hardware-тестов — явно разрешённое тестовое устройство и совместимый
  драйвер.

```powershell
git clone https://github.com/kryshkoilya/ALGA-VECTOR.git
cd ALGA_VECTOR
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,hardware]"
```

Проверка без открытия spectrum capture:

```powershell
.\.venv\Scripts\python.exe -m alga_vector --hardware-preflight
```

Первый запуск разработки:

```powershell
.\.venv\Scripts\python.exe -m alga_vector --demo
.\.venv\Scripts\python.exe -m alga_vector --safe
```

## Ветка и размер изменения

- Создавайте отдельную ветку от актуальной основной ветки.
- Один pull request должен решать одну связанную проблему.
- Не смешивайте массовое форматирование с функциональным изменением.
- Не коммитьте `.venv`, `build`, `dist`, runtime-data, логи, базы, IQ-записи,
  support bundles или пользовательскую конфигурацию.
- Не добавляйте бинарный dataset без описания происхождения, согласия,
  лицензии и privacy review.

Пример имён веток:

```text
fix/device-reconnect
feat/simple-mode-accessibility
test/signal-processor-policy
docs/hardware-acceptance
```

## Стиль кода

- Python 3.12.
- Полная типизация нового production-кода.
- Ruff и strict Mypy должны проходить без локальных исключений «на всякий
  случай».
- Domain/state objects предпочтительно делать immutable.
- Ошибки на границах hardware/I/O преобразуются в структурированные,
  actionable diagnostics.
- Ограничивайте очереди, время subprocess, объём output и историю событий.
- Не блокируйте Qt UI thread acquisition или тяжёлым DSP.
- Сохраняйте русскоязычные operator-facing тексты короткими и однозначными.
- Технические детали размещайте в EXPERT MODE.

## Проверки перед pull request

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src\alga_vector
.\.venv\Scripts\python.exe -m pytest --basetemp .build-temp\pytest
.\.venv\Scripts\python.exe -m alga_vector --hardware-preflight
.\.venv\Scripts\python.exe -m alga_vector --demo --headless-smoke --skip-onboarding
.\.venv\Scripts\python.exe -m alga_vector --safe --headless-smoke --skip-onboarding
```

Для UI-изменений добавьте offscreen Qt-тест и приложите снимок до/после. Для
изменений packaging выполните:

```powershell
.\packaging\build.ps1 -SkipInstaller
```

Release-сборку нельзя объявлять проверенной, если она собрана с `-SkipTests`.

## Требования к тестам

### Device/Acquisition

Покройте как минимум:

- отсутствие зависимости или host tool;
- timeout и bounded output;
- занятое устройство;
- open/read/configuration failure;
- malformed payload;
- disconnect/reconnect;
- отсутствие silent reuse повреждённого handle;
- sequence и retune boundaries.

Hardware-тесты должны иметь marker `hardware` и запускаться только при явно
подключённом тестовом устройстве.

### Signal processing

Покройте:

- quiet background;
- одиночный spike;
- устойчивый эпизод;
- dropout и stale;
- смену частоты/окна;
- недостаточное качество;
- противоречащие признаки;
- release/holding;
- abstention;
- недопустимое identity-событие.

### UI

Проверьте:

- SIMPLE MODE не импортирует и не интерпретирует raw RF;
- переключение режима не сбрасывает runtime state;
- отсутствующий сенсор имеет human-readable fallback;
- Demo всегда визуально маркирован;
- важное событие не вытесняется информационным потоком;
- маленькое окно не перекрывает критические элементы;
- ошибка snapshot/processor видна, а не скрыта.

## Вклад нового сенсора

Новый источник подключается через отдельный adapter/ingestion boundary:

1. Опишите capability и единицы измерения.
2. Введите строгую schema и provenance.
3. Ограничьте freshness и допустимое качество.
4. Не смешивайте discovery с capture.
5. Добавьте fail-closed validation.
6. Нормализуйте событие через `signal_processor`.
7. Опишите, что источник может и чего не может доказать.
8. Добавьте fake-source для deterministic Demo.
9. Добавьте отрицательные и failure-path тесты.
10. Укажите отдельный hardware acceptance plan.

Если новый источник выдаёт identity-like классификацию, pull request должен
содержать versioned model identifier, model card, происхождение dataset,
разделение train/validation без leakage, calibration report и заранее
определённые error metrics. Иначе допустим только нейтральный тип наблюдения.

## Изменение event schema

- Не переиспользуйте старое значение с новым смыслом.
- Обновляйте schema version и migration/compatibility path при изменении
  контракта.
- Сохраняйте source attribution, timestamps, provenance, limitations и
  supporting/contradicting evidence.
- Новое severity должно иметь понятное operator action.
- Dedup key обязан быть устойчивым и не скрывать новый реальный эпизод.
- Проверьте priority retention event bus под информационным flood.

## Документация hardware-утверждений

Аппаратное утверждение должно ссылаться на:

- конкретную модель и revision;
- драйвер, firmware и host tool version;
- Windows build и USB-контроллер;
- антенну/кабель/аттенюатор/LNA;
- режим gain и единицу уровня;
- сценарий и ожидаемый результат;
- raw evidence или воспроизводимую процедуру;
- границы проверки.

Успешный mock/fake test нельзя описывать как физическую совместимость.
Descriptor discovery нельзя описывать как успешный capture.

## Pull request

В описании укажите:

- проблему и наблюдаемое поведение;
- выбранное решение и альтернативы;
- затронутые слои;
- изменения schema/config;
- влияние на SIMPLE и EXPERT;
- тесты и их фактический результат;
- hardware, которое действительно использовалось;
- новые ограничения и остаточные риски;
- screenshots для UI;
- migration/rollback plan.

Минимальный checklist:

```text
[ ] Live не подменяется Demo
[ ] Частота/RSSI не используются как identity proof
[ ] Ошибки видимы и структурированы
[ ] Ruff проходит
[ ] strict Mypy проходит
[ ] Pytest проходит
[ ] Safe и Demo smoke проходят
[ ] Документация обновлена
[ ] Чувствительные данные не добавлены
[ ] Hardware claims отделены от mock evidence
```

## Сообщения коммитов

Используйте короткое повелительное описание, например:

```text
fix: invalidate RTL-SDR handle after malformed IQ
feat: add sensor-unavailable recommendation
test: cover event-bus alarm retention
docs: document external DF acceptance gate
```

## Ошибки и уязвимости

Обычные воспроизводимые ошибки можно создавать в Issues без персональных,
координатных и чувствительных RF-данных. Уязвимости, утечки, обход policy gate
и небезопасное управление устройством сообщайте приватно по
[`SECURITY.md`](SECURITY.md).

## Лицензирование вкладов

На дату подготовки этого документа в дереве проекта не найден корневой файл
`LICENSE`. Публичная доступность исходников сама по себе не предоставляет
разрешение на использование, изменение или распространение.

Владельцу репозитория следует выбрать и добавить явную лицензию до приёма
внешних вкладов. До этого момента не отправляйте код, права на который вы не
можете предоставить, и отдельно согласуйте условия вклада с владельцем
репозитория.
