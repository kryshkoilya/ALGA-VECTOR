# ALGA VECTOR — исторические Windows-сборки 0.2.1–0.6.1

Этот GitHub Release сохраняет проверяемые исполняемые артефакты ранних версий
ALGA VECTOR. Он предназначен для истории и воспроизводимости, а не для новой
установки.

## Важно

- Используйте актуальный поддерживаемый релиз `0.7.0`.
- Версии 0.2.1–0.6.1 больше не получают исправления безопасности.
- Исходные git commits этих версий в доступном workspace не сохранились.
- Архивы являются **legacy binaries; source snapshot unavailable**.
- Они не имеют Authenticode-подписи. Перед запуском обязательно сверяйте
  SHA-256.
- Не запускайте две версии одновременно с одним SDR или COM-портом.
- Старые Demo-сценарии не являются аппаратной или полевой валидацией.

Отдельные теги `v0.2.1`–`v0.6.1` намеренно не создаются. Если бы они указывали
на текущий commit 0.7.0, автоматически созданные GitHub source archives
содержали бы неверную версию исходников.

Сам release привязан только к marker-тегу `legacy-binaries-2026-07-26`.
Автоматические ссылки GitHub **Source code (zip/tar.gz)** возле этого тега
являются снимком marker-коммита и **не являются исходниками 0.2.1–0.6.1**.
Историческими артефактами считаются только явно перечисленные ниже Windows ZIP
и соответствующие им `.sha256.txt`.

## Артефакты

| Версия | ZIP | SHA-256 |
|---|---|---|
| 0.2.1 | `ALGA_VECTOR-0.2.1-Windows-x64-onedir.zip` | `964CB42930851558B7B283C0465F16C9C82F317C0713154D644A9419F8D090D4` |
| 0.3.0 | `ALGA_VECTOR-0.3.0-Windows-x64-onedir.zip` | `8A99086E25D88848D379C25A66F0B3E3C52B558939158E7C5E46BCCCC3C32235` |
| 0.4.0 | `ALGA_VECTOR-0.4.0-Windows-x64-onedir.zip` | `049FDBC19C4E49F450551EB881CDA79C0FA6A81756A3B331599C26FEFD1ECDF5` |
| 0.4.1 | `ALGA_VECTOR-0.4.1-Windows-x64-onedir.zip` | `B79442F5E17FAF7C781601581B8CE299DF542CBE6C657FB16B697594A3ABE808` |
| 0.5.0 | `ALGA_VECTOR-0.5.0-Windows-x64-onedir.zip` | `135D1E15488B9B9AD982C811F5474CA1369EC161A7A7E827B6E723CA704E4B44` |
| 0.6.0 | `ALGA_VECTOR-0.6.0-Windows-x64-onedir.zip` | `861DB6FB57F84D23A151A7B964C8442BAC7BD1EC9D4D48ECD4BBAC4D0D6CE3B5` |
| 0.6.1 | `ALGA_VECTOR-0.6.1-Windows-x64-onedir.zip` | `959EA2A6B0553475AFF6DB1EA1E304EA857D27996701A4E181CAE70FBF8AE26A` |

Каждый ZIP публикуется вместе с одноимённым `.sha256.txt`.

## Краткая эволюция

- **0.2.1** — ранний запускаемый Windows-инкремент; подробный source/release
  report не сохранился.
- **0.3.0** — Live по умолчанию, явный Demo, RTL-SDR discovery, ранние
  novice/expert и карта.
- **0.4.0** — аппаратный worker, config v4, spectrum/events/map/diagnostics,
  support bundle.
- **0.4.1** — temporal RF FSM, debounce/hysteresis и устранение алерта по
  одному FFT-всплеску.
- **0.5.0** — receive-only RF workflow, HackRF/tinySA capability contracts,
  fail-closed Direction.
- **0.6.0** — запускаемый мультисенсорный foundation: acoustic, гражданский
  ADS-B context и temporal fusion.
- **0.6.1** — capability-gated последовательный автообзор и объяснимые
  RF-события.

Полное описание evidence, границ и checksum находится в
[`VERSIONS.md`](../VERSIONS.md).
