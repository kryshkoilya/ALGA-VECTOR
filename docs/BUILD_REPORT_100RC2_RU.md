# ALGA VECTOR 1.0.0rc2 — отчёт сборки

Дата локального release gate: 2 августа 2026 года.

## Артефакт

- portable ZIP:
  `dist/ALGA_VECTOR-1.0.0rc2-Windows-x64-onedir.zip`;
- размер: `72 829 367` байт;
- записей в ZIP: `938`;
- checksum-файл:
  `dist/ALGA_VECTOR-1.0.0rc2-Windows-x64-onedir.zip.sha256.txt`;
- SHA-256:
  `C4B1024D7A1AB8D2CBE590004F8C90DE0C2A4BB735360A0A3D3AC7AF73C835AE`;
- GUI FileVersion/ProductVersion: `1.0.0rc2`;
- CLI FileVersion/ProductVersion: `1.0.0rc2`;
- numeric PE fixed version: `1.0.0.0`.

## Release gate

| Проверка | Результат |
|---|---|
| Ruff `src tests` | PASS |
| strict Mypy `src/alga_vector` | PASS, 115 файлов |
| Pytest | PASS, 559 тестов |
| Source hardware preflight | PASS |
| Source default Live smoke | PASS |
| Source explicit Live smoke | PASS |
| Source Safe smoke | PASS |
| Source Demo smoke | PASS |
| PyInstaller GUI/CLI onedir | PASS |
| GUI/CLI textual и fixed PE versions | PASS |
| Frozen hardware preflight | PASS |
| Frozen CLI default Live smoke | PASS |
| Frozen CLI explicit Live smoke | PASS |
| Frozen CLI Safe smoke | PASS |
| Frozen CLI Demo smoke | PASS |
| Frozen GUI default Live smoke | PASS |
| Frozen GUI Safe smoke | PASS |
| Portable extract + preflight | PASS |
| Portable extract + Safe smoke | PASS |
| SHA-256 после QA | PASS |
| Inno Setup installer | SKIPPED (`-SkipInstaller`) |

## Специальные регрессионные проверки rc2

- короткий RF-всплеск после изученного фона попадает в UnifiedEventBus без
  запроса UI snapshot;
- следующий тихий кадр не удаляет уже опубликованное generic RF-событие;
- `IDLE/UNKNOWN` с измеренной активностью не превращается в `NOISE_BACKGROUND`;
- suppressed и low-confidence наблюдения остаются
  `RADIO_ACTIVITY_DETECTED`, а не `SENSOR_UNAVAILABLE`;
- generic RF-событие не получает identity и не становится
  `LIKELY_DRONE_SIGNATURE`/`TARGET_CONFIRMED`;
- SIMPLE MODE показывает generic activity при включённом фильтре важных событий;
- `field_priority` для RTL-SDR исключает 2,4/5,8 ГГц и не создаёт окна выше
  аппаратного максимума;
- совместимый HackRF включает верхние участки в bounded-план;
- слишком широкий план не сохраняется для startup resume;
- startup resume повторно проходит capability compilation;
- RTL-SDR open errors различают descriptor absent, busy, access denied и
  driver/backend failure;
- device state становится `STREAMING` только после принятого кадра;
- `--debug` включает DEBUG только для текущего процесса и не перезаписывает
  сохранённый уровень профиля;
- `trace_id` не ломает exact/semantic dedup целей.

## Hardware preflight на машине сборки

- обязательные Python-модули `pyserial`, `pyrtlsdr`, `pyusb`: доступны;
- физический RTL-SDR во время release gate: descriptor не подключён;
- optional `hackrf_info` / `hackrf_transfer`: отсутствуют;
- tinySA metadata-кандидаты: не найдены.

Следовательно, release gate подтверждает программный тракт, frozen packaging и
детерминированные regression tests, но не является физической приёмкой
конкретного SDR, драйвера, антенны или полевого объекта.

## Команда воспроизведения

```powershell
$env:PYTHONPATH='.venv\Lib\site-packages;src'
& .\packaging\build.ps1 -SkipInstaller -PythonPath '<Python 3.12 x64>'
```

Build script сначала выполняет проверки, затем создаёт onedir, проверяет оба
EXE, формирует candidate ZIP, распаковывает его в отдельный каталог и только
после успешного smoke публикует ZIP и checksum в `dist`.

Разработал: Буйвол и Задира
