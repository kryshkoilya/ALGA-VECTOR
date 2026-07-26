# ALGA VECTOR 0.6.0 — отчёт о первом запускаемом инкременте

Дата проверки: 26 июля 2026 года.

Подпись продукта: **«Разработал: Буйвол и Задира»**.

## Результат

Этап F собран как локально запускаемый Windows onedir-релиз, а не только как
архитектурный план. Поставлены:

- PySide6 shell;
- Dashboard с объяснимым мультисенсорным итогом;
- Devices page;
- fake device adapters и детерминированный Demo;
- rotating structured JSONL logs;
- onboarding wizard;
- явные Live, Safe и Demo;
- PyInstaller GUI/CLI;
- portable ZIP и Inno Setup skeleton.

Дополнительно интегрированы акустический PCM analysis core, локальный
гражданский ADS-B context и temporal sensor fusion с abstention, hysteresis и
объяснимой цепочкой evidence.

## Фактический quality gate

| Проверка | Результат |
|---|---|
| Ruff `src tests` | пройдено |
| Strict Mypy | 92 source-файла, ошибок нет |
| Pytest | 368 тестов пройдено |
| Source smoke | default-live, explicit-live, safe, demo пройдены |
| Hardware preflight | пройден без открытия capture |
| PyInstaller | GUI и CLI собраны |
| Windows versions | FileVersion/ProductVersion `0.6.0` для обоих EXE |
| Frozen CLI smoke | preflight, default-live, explicit-live, safe, demo пройдены |
| Frozen GUI smoke | default-live и safe пройдены |
| Portable extraction smoke | распаковка и safe smoke пройдены |
| SHA-256 | вычислен и повторно совпал |

Физические RTL-SDR, HackRF и tinySA в этом прогоне не были подключены.
`hackrf_info`/`hackrf_transfer` отсутствовали. Это не мешает Demo/Safe и не
заменяет отдельный аппаратный acceptance test.

## Релизный артефакт

Файл:

`dist/ALGA_VECTOR-0.6.0-Windows-x64-onedir.zip`

Размер: `64 314 106` байт.

SHA-256:

`861DB6FB57F84D23A151A7B964C8442BAC7BD1EC9D4D48ECD4BBAC4D0D6CE3B5`

Контрольная сумма также сохранена в:

`dist/ALGA_VECTOR-0.6.0-Windows-x64-onedir.zip.sha256.txt`

## Как запустить

1. Распаковать ZIP в отдельный каталог.
2. Для полностью автономной проверки выполнить:

   `ALGA VECTOR\ALGA VECTOR.exe --demo`

3. Для запуска без real adapters выполнить:

   `ALGA VECTOR\ALGA VECTOR.exe --safe`

4. Обычный двойной клик запускает Live, но открывает только явно настроенные и
   включённые adapters.

В каталог приложения вложены `README_FIRST_RU.txt` и `RELEASE_NOTES_RU.md`.

## Исправление packaging reliability

Во время gate старая запущенная onedir-сборка удерживала DLL NumPy и мешала
PyInstaller атомарно заменить каталог. `packaging/build.ps1` доработан:
перед сборкой он проверяет только процессы из собственного
`dist/ALGA VECTOR`, перечисляет их PID и выдаёт понятную ошибку. После закрытия
старого экземпляра полный packaging gate прошёл.

## Визуальный smoke

Снимок реального PySide6 Demo-dashboard после трёх детерминированных
наблюдений:

`docs/demo-dashboard-060.png`

На нём проверены:

- явная маркировка `ДЕМО · СИМУЛЯЦИЯ`;
- общий мультисенсорный вывод;
- четыре отдельные sensor-state ячейки;
- отсутствие утверждений о типе, координатах, намерении или принадлежности
  физического источника.

## Честные ограничения foundation

- bundled Windows microphone capture backend ещё не поставлен; доступен
  проверяемый PCM ingestion contract;
- локальный ADS-B parser читает готовый `aircraft.json`, но не управляет
  dump1090;
- bundled adapter конкретного внешнего DF-источника отсутствует;
- Inno Setup installer не компилировался в этом прогоне; skeleton готов,
  основной проверенный артефакт — portable ZIP;
- code signing, clean-VM acceptance, physical hardware matrix и длительный
  soak относятся к hardening-инкременту.

