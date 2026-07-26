# ALGA VECTOR 0.7.0 — отчёт сборки

Дата проверки: 2026-07-26  
Целевая платформа: Windows 11 x64, AMD64  
Результат release gate: **PASS**

## Готовый артефакт

- файл: `dist/ALGA_VECTOR-0.7.0-Windows-x64-onedir.zip`;
- размер: 64 573 045 байт, около 61,58 MiB;
- SHA-256:
  `B40CF7C9CC6A0FBF9556888265D791DD8566C5CF4FDF7C7A7916BFA2E2A5316E`;
- checksum-файл:
  `dist/ALGA_VECTOR-0.7.0-Windows-x64-onedir.zip.sha256.txt`;
- элементов в ZIP: 292;
- GUI FileVersion/ProductVersion: `0.7.0`;
- CLI FileVersion/ProductVersion: `0.7.0`.

Checksum пересчитан после сборки и совпадает с опубликованным значением.
В архиве проверено наличие GUI, CLI, `README_FIRST_RU.txt` и
`RELEASE_NOTES_RU.md`.

Архив публиковался только после распаковки временного кандидата, запуска
hardware preflight и Safe headless smoke из распакованной копии.

Сборка выполнена с `-SkipInstaller`: Inno Setup 6 в среде отсутствует.
Проверенный артефакт этого отчёта — portable onedir ZIP.

## Автоматические проверки

| Проверка | Результат |
|---|---|
| Ruff | PASS |
| strict Mypy | PASS, 104 source-файла |
| pytest | PASS, 460 тестов |
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
| EXPERT MODE spectrum render 1440×900 | PASS, визуально проверен |

Все source/frozen smoke-проверки запускались с изолированными `TEMP` и
LocalAppData. Они не использовали пользовательский профиль и не требовали
завершать ранее открытый экземпляр ALGA VECTOR 0.6.1.

## Среда сборки

- Windows `10.0.26200`, определяется как Windows 11;
- Python `3.12.10`, AMD64;
- PyInstaller `6.21.0`;
- PySide6 Essentials `6.11.1`;
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

Build script проверил совпадение версии package, `pyproject.toml`, metadata
установщика, quick-start, release notes и PE resources. Имя Windows-x64
разрешается только при AMD64/64-bit Python.

## Что подтверждено в 0.7.0

- один runtime обслуживает SIMPLE MODE и EXPERT MODE без перезапуска
  acquisition, порогов или temporal state;
- SIMPLE MODE открывается на вкладке `Простая обстановка` и читает только
  `operator_situation`;
- технические страницы сохранены и доступны в EXPERT MODE;
- новый `signal_processor/` нормализует RF, acoustic, ADS-B context,
  direction и fusion в versioned event schema;
- bounded thread-safe event bus изолирует ошибки подписчиков, подавляет
  семантические дубликаты и не позволяет информационному потоку вытеснить
  тревожные события;
- отсутствующий сенсор получает явный `SENSOR_UNAVAILABLE` и понятный
  fallback;
- глобальная плашка использует нормализованный primary event и не
  «воскрешает» устаревший legacy RF/fusion alert;
- состояние качества RF не засоряет операторский журнал одинаковыми
  предупреждениями;
- направление отображается только из свежего внешнего валидированного DF;
- ручной и demo-азимут явно исключаются из измеренного operator bearing;
- failure `signal_processor` создаёт видимый incident, снижает readiness и
  не подменяется интерпретацией raw SDR в UI;
- смена operator situation и режим интерфейса пишутся в structured logs;
- интерфейсные режимы, фильтр важных событий и основной operator flow
  покрыты автоматическими UI-тестами.

## Аппаратный preflight

Preflight обнаружил descriptor `Generic RTL2832U OEM (RTLSDR:0)` и подтвердил
наличие Python runtime-модулей pyserial, pyrtlsdr и pyusb.

Это подтверждает перечисление descriptor и готовность программного runtime,
но не является полевой проверкой чувствительности, калибровки, антенны,
динамического диапазона или достоверности классификации.

Дополнительно:

- `hackrf_info` и `hackrf_transfer` отсутствуют, поэтому HackRF discovery не
  выполнялся;
- metadata-кандидаты tinySA не найдены;
- внешний KrakenSDR/DF не подключён и не калибровался;
- акустический live-вход и физическая ADS-B сеть в этом release gate не
  проверялись.

## Что не заявляется

- Частота, RSSI или спектральная форма сами по себе не доказывают
  `дрон / рация / видеоканал` и не устанавливают модель, оператора,
  назначение или национальную принадлежность.
- Confidence — эвристическая сила доступных признаков, а не калиброванная
  вероятность физического класса.
- Один tinySA, RTL-SDR или HackRF не измеряет bearing.
- RSSI не преобразуется в расстояние.
- Один bearing не преобразуется в координаты источника.
- ADS-B является гражданским cooperative context, а не IFF.
- Полевая probability of detection и false-alarm rate не заявлены без
  размеченного validation dataset и protocol-specific model card.

## Открытые acceptance-риски

Перед применением на конкретном объекте нужна отдельная аппаратная матрица:
Windows build, драйвер, firmware, USB-контроллер, питание, антенна,
аттенюатор/LNA, допустимый входной уровень, частотный профиль и реальные
интервалы между кадрами.

Для внешнего DF нужны действующая калибровка массива, проверка ориентации и
контроль срока действия evidence. Для RF/acoustic fusion нужны размеченные
полевые записи, независимая ground truth и отдельные метрики false-positive /
false-negative.

В projectless workspace нет git commit/source manifest, SBOM, lock-файла и
Authenticode-подписи. Они остаются отдельными release-задачами.
