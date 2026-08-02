# ALGA VECTOR — сторонние компоненты

Этот файл фиксирует сторонние компоненты, используемые исходным кодом и
portable-сборкой ALGA VECTOR 1.0.0rc1 для Windows x64. Он не заменяет тексты
соответствующих лицензий и не является юридической консультацией.

## Важное различие

Публикация исходного кода ALGA VECTOR в GitHub сама по себе не означает выдачу
открытой лицензии на код продукта. Отдельная лицензия проекта владельцами пока
не выбрана. У каждого перечисленного ниже стороннего компонента сохраняются его
собственные условия.

## Runtime и UI

| Компонент | Версия в проверенной сборке 1.0.0rc1 | Лицензия по metadata/upstream | Назначение |
|---|---:|---|---|
| CPython | 3.12.10 | PSF License Agreement | встроенный Python runtime |
| OpenSSL | 3.0.16 | Apache License 2.0 | TLS-библиотеки, входящие в Python runtime |
| PySide6 Essentials / Qt | 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; также доступно коммерческое лицензирование Qt | desktop UI и Qt runtime |
| shiboken6 | 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Python/Qt bindings runtime |
| NumPy | 2.5.1 | BSD-3-Clause и лицензии включённых компонентов | численные операции |
| Pydantic | 2.13.4 | MIT | валидация моделей |
| pydantic-core | 2.46.4 | MIT | runtime Pydantic |
| PyYAML | 6.0.3 | MIT | конфигурация YAML |
| platformdirs | 4.11.0 | MIT | системные каталоги данных |
| annotated-types | 0.8.0 | MIT | runtime Pydantic |
| typing-extensions | 4.16.0 | PSF-2.0 | совместимость typing |
| typing-inspection | 0.4.2 | MIT | runtime Pydantic |

Официальные источники:

- Python: <https://www.python.org/psf/license/>
- OpenSSL: <https://www.openssl.org/source/license.html>
- Qt for Python: <https://doc.qt.io/qtforpython/licenses.html>
- Qt licensing: <https://www.qt.io/licensing/>
- NumPy: <https://github.com/numpy/numpy>
- Pydantic: <https://github.com/pydantic/pydantic>
- PyYAML: <https://github.com/yaml/pyyaml>
- platformdirs: <https://github.com/tox-dev/platformdirs>

## Аппаратные адаптеры

| Компонент | Версия | Лицензия по metadata/upstream | Назначение |
|---|---:|---|---|
| pyserial | 3.5 | BSD-3-Clause | serial/COM |
| pyrtlsdr | 0.5.0 | GPL-3.0-or-later | Python API RTL-SDR |
| pyrtlsdrlib | 0.0.5 | MIT | поставка нативных библиотек RTL-SDR |
| PyUSB | 1.3.1 | BSD-3-Clause | USB backend |
| rtl-sdr native library | входит через pyrtlsdrlib | GPL-2.0-or-later по upstream rtl-sdr | доступ к RTL-SDR |
| libusb | входит в цепочку RTL-SDR/USB | LGPL-2.1-or-later по upstream libusb | USB transport |

Официальные источники:

- pyserial: <https://github.com/pyserial/pyserial>
- pyrtlsdr: <https://github.com/pyrtlsdr/pyrtlsdr>
- pyrtlsdrlib: <https://github.com/pyrtlsdr/pyrtlsdrlib>
- PyUSB: <https://github.com/pyusb/pyusb>
- rtl-sdr: <https://github.com/osmocom/rtl-sdr>
- libusb: <https://github.com/libusb/libusb>

## Сборка и визуальные ресурсы

| Компонент | Версия | Лицензия | Примечание |
|---|---:|---|---|
| PyInstaller | 6.21.0 | GPL-2.0-or-later с Bootloader Exception | инструмент сборки; bootloader входит в EXE |
| Golos Text | bundled asset | SIL Open Font License 1.1 | шрифт интерфейса |

Полный текст OFL для Golos Text хранится в
[`src/alga_vector/assets/fonts/OFL-Golos-Text.txt`](src/alga_vector/assets/fonts/OFL-Golos-Text.txt).
Информация PyInstaller: <https://pyinstaller.org/en/stable/license.html>.

## Где находятся тексты в portable-архиве

Portable-сборка сохраняет доступные license-файлы Python-пакетов рядом с их
`*.dist-info` внутри каталога `_internal`. В частности, там присутствуют
лицензии NumPy, Pydantic, PyUSB и pyrtlsdrlib. Этот файл копируется в корень
portable-пакета как `THIRD_PARTY_NOTICES.md`.

Перед внешним коммерческим распространением владелец релиза должен отдельно
проверить выбранную модель лицензирования Qt/PySide6, полноту license-текстов,
обязательства copyleft-компонентов и применимость условий к конкретному способу
распространения.
