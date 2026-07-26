# ALGA VECTOR 0.6.0 — первый мультисенсорный foundation

Дата документа: 26 июля 2026 года.

Подпись продукта: **«Разработал: Буйвол и Задира»**.

## Результат

ALGA VECTOR 0.6.0 — локально запускаемый Windows foundation гражданской
мультисенсорной системы раннего предупреждения. Это рабочий программный
инкремент, на котором можно проверять пользовательский сценарий, контракты
сенсоров и объяснимую temporal fusion без подключённого оборудования.

В поставку входят:

- PySide6 shell с русскоязычной навигацией;
- dashboard и страница устройств;
- onboarding wizard;
- явные Live, Safe и Demo;
- детерминированные fake-источники для Demo;
- rotating structured JSONL logs и локальная диагностика;
- существующий receive-only RF-контур;
- PCM feature/detection core для акустических наблюдений;
- парсер локального гражданского `dump1090 aircraft.json`;
- temporal multi-sensor fusion core с abstention;
- PyInstaller onedir, portable ZIP и Inno Setup skeleton для Windows.

Архитектурный и UX-контракт этапов A–E описан в
`docs/ALGA_VECTOR_060_FOUNDATION_RU.md`. Этот документ фиксирует первый
реализуемый инкремент этапа F и его честные границы.

## Контракт данных

```text
RF observation ───────────┐
acoustic observation ─────┼─> freshness/quality gates ─> temporal fusion
civilian ADS-B context ───┤                               │
validated bearing ────────┘                               └─> explanation/abstention
```

- Один всплеск не считается достаточным подтверждением.
- Stale, malformed и неполные данные изолируются и не превращаются в факт.
- ADS-B используется только как гражданский контекст, не как IFF.
- Bearing принимается только от отдельного валидированного источника.
- Demo-наблюдения всегда имеют simulated provenance.
- Если доказательств недостаточно, корректный результат — «недостаточно
  данных», а не догадка.

## Что реально можно запустить

Из исходников:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,hardware]"

# Детерминированный сценарий без оборудования
.\.venv\Scripts\python.exe -m alga_vector --demo

# Без открытия real adapters
.\.venv\Scripts\python.exe -m alga_vector --safe

# Live: только явно настроенные и включённые источники
.\.venv\Scripts\python.exe -m alga_vector --live
```

Повторный onboarding:

```powershell
.\.venv\Scripts\python.exe -m alga_vector --onboarding
```

Demo предназначен для проверки shell, dashboard, devices, журналирования и
fusion-сценария. Он не является доказательством чувствительности реальных
сенсоров.

## Что изменилось относительно 0.5.0

- Продуктовая модель расширена от RF-only к модульному гражданскому
  multi-sensor foundation.
- Добавлены отдельные контракты acoustic, civilian airspace context и fusion.
- Конфигурация повышена до `schema_version: 5`; новые live-входы по умолчанию
  выключены.
- Объяснение решения отделяет качество данных, поддержку наблюдения,
  противоречия и недостающее подтверждение.
- Dashboard ориентирован на состояние всей сенсорной цепочки, а не одного
  графика.
- Demo даёт воспроизводимый end-to-end сценарий без скрытой подмены Live.
- Windows build вкладывает этот документ как `RELEASE_NOTES_RU.md`.

## Граница достоверности

Версия 0.6.0 не заявляет:

- bundled live-захват с микрофона;
- обученную и полевым образом валидированную модель конкретного физического
  объекта;
- физическую проверку RF-приёмников, микрофонов или ADS-B-приёмника на этой
  сборке;
- полноту или истинность стороннего ADS-B-потока;
- определение страны, национальности, принадлежности, намерения, «свой/чужой»
  или IFF;
- идентификацию военной платформы;
- координаты, дальность, траекторию, точную геолокацию или автоматическое
  целеуказание;
- направление без отдельного свежего калиброванного DF-источника;
- передачу, подавление, постановку помех или управление радиосредствами;
- сертифицированную вероятность обнаружения или ложной тревоги.

Акустический модуль в этом foundation принимает PCM через программный
контракт. Подключение конкретного Windows audio backend — отдельный следующий
инкремент. ADS-B-модуль читает уже созданный локальный JSON-файл и не запускает
и не администрирует dump1090.

## Проверка

Фактический source gate 26 июля 2026 года:

- Ruff: без замечаний;
- strict Mypy: 92 source-файла без ошибок;
- Pytest: 368 тестов пройдено;
- отдельные UI-регрессии проверяют мультисенсорную карточку и fail-closed
  отображение ошибки runtime.

Source release gate:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src\alga_vector
.\.venv\Scripts\python.exe -m pytest --basetemp .build-temp\pytest
.\.venv\Scripts\python.exe -m alga_vector --hardware-preflight
.\.venv\Scripts\python.exe -m alga_vector --demo --headless-smoke --skip-onboarding
.\.venv\Scripts\python.exe -m alga_vector --safe --headless-smoke --skip-onboarding
```

Результат конкретного запуска тестов следует брать из build log. Само наличие
этого документа не означает, что физическое оборудование было подключено.

## Windows packaging

Onedir без installer и portable ZIP:

```powershell
.\packaging\build.ps1 -SkipInstaller -SkipPortable
```

Onedir и проверяемый portable ZIP:

```powershell
.\packaging\build.ps1 -SkipInstaller
```

Полная сборка installer требует установленный Inno Setup 6:

```powershell
.\packaging\build.ps1
```

Скрипт проверяет версии GUI/CLI EXE, выполняет source/frozen smoke, распаковывает
portable ZIP для повторного smoke и формирует SHA-256. Сборка с `-SkipTests`
предназначена только для локальной итерации и не является release gate.

## Следующий production-инкремент

- explicit Windows microphone adapter с capability/provenance и disconnect
  handling;
- controlled ingestion внешнего Kraken/DF API;
- replay размеченных гражданских записей;
- calibration, soak и disconnect/reconnect на физической sensor matrix;
- измерение latency, missed observations и false-alarm rate на заранее
  определённом validation dataset;
- чистая Windows VM, installer acceptance и при необходимости Authenticode.
