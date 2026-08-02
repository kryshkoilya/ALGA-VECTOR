from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import alga_vector.__main__ as cli
from alga_vector.__main__ import _mode_from_args, build_parser
from alga_vector.bootstrap import _apply_runtime_overrides, _persistable_config
from alga_vector.config import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    LocationPolicyConfig,
    MapConfig,
)
from alga_vector.devices import has_enabled_real_hardware
from alga_vector.resources import default_config_path


def test_default_config_is_bundled() -> None:
    assert default_config_path().is_file()


def test_relative_data_dir_is_scoped_to_user_data(tmp_path: Path) -> None:
    config = AppConfig()

    effective = _apply_runtime_overrides(
        config,
        user_data_dir=tmp_path,
        mode_override="safe",
        data_dir_override=Path("captures"),
    )

    assert effective.mode == "safe"
    assert effective.storage.data_dir == (tmp_path / "captures").resolve()
    assert effective.storage.data_dir.is_dir()


def test_process_overrides_are_not_persisted(tmp_path: Path) -> None:
    base = AppConfig(mode="live", devices=DevicesConfig(enable_real_adapters=True))
    effective = _apply_runtime_overrides(
        base,
        user_data_dir=tmp_path,
        mode_override="safe",
        data_dir_override=tmp_path / "temporary",
    ).model_copy(update={"first_run_complete": True})

    persisted = _persistable_config(
        effective,
        base=base,
        mode_overridden=True,
        data_dir_overridden=True,
    )

    assert persisted.first_run_complete is True
    assert persisted.mode == "live"
    assert persisted.devices.enable_real_adapters is True
    assert persisted.storage.data_dir == base.storage.data_dir


def test_live_receiver_settings_remain_persistable() -> None:
    base = AppConfig(mode="demo")
    candidate = AppConfig(
        mode="live",
        devices=DevicesConfig(
            enable_real_adapters=True,
            adapters=[
                AdapterConfig(
                    id="physical",
                    kind="tinysa",
                    enabled=True,
                    connection="COM7",
                )
            ],
        ),
    )

    persisted = _persistable_config(
        candidate,
        base=base,
        mode_overridden=False,
        data_dir_overridden=False,
    )

    assert persisted.mode == "live"
    assert persisted.devices.enable_real_adapters is True
    assert [adapter.connection for adapter in persisted.devices.adapters] == ["COM7"]


def test_network_map_override_is_process_only(tmp_path: Path) -> None:
    base = AppConfig(map=MapConfig(network_enabled=True, default_zoom=11))
    effective = _apply_runtime_overrides(
        base,
        user_data_dir=tmp_path,
        mode_override="live",
        data_dir_override=None,
        network_maps_override=False,
    )

    assert effective.map.network_enabled is False
    candidate = effective.model_copy(
        update={"map": effective.map.model_copy(update={"default_zoom": 14})}
    )
    persisted = _persistable_config(
        candidate,
        base=base,
        mode_overridden=False,
        data_dir_overridden=False,
        network_maps_overridden=True,
    )

    assert persisted.map.network_enabled is True
    assert persisted.map.default_zoom == 14


def test_v05_runtime_disables_legacy_map_network_and_gps_profile(
    tmp_path: Path,
) -> None:
    base = AppConfig(
        map=MapConfig(network_enabled=True),
        location=LocationPolicyConfig(source="gps", gps_port="COM7"),
    )

    effective = _apply_runtime_overrides(
        base,
        user_data_dir=tmp_path,
        mode_override="live",
        data_dir_override=None,
    )

    assert effective.map.network_enabled is False
    assert effective.location.source == "unset"
    assert effective.location.gps_port == ""


def test_headless_smoke_passes_network_map_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def stop_after_capture(**kwargs: object) -> object:
        received.update(kwargs)
        raise RuntimeError("stop after bootstrap arguments")

    monkeypatch.setattr(cli, "build_context", stop_after_capture)
    args = build_parser().parse_args(["--headless-smoke", "--skip-onboarding"])

    assert cli._run(args) == 2
    assert received["network_maps_override"] is False


def test_debug_cli_enables_process_debug_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def stop_after_capture(**kwargs: object) -> object:
        received.update(kwargs)
        raise RuntimeError("stop after bootstrap arguments")

    monkeypatch.setattr(cli, "build_context", stop_after_capture)
    args = build_parser().parse_args(
        ["--debug", "--headless-smoke", "--skip-onboarding"]
    )

    assert cli._run(args) == 2
    assert received["debug_logging_override"] is True


def test_debug_logging_override_is_process_only(tmp_path: Path) -> None:
    base = AppConfig()
    effective = _apply_runtime_overrides(
        base,
        user_data_dir=tmp_path,
        mode_override="live",
        data_dir_override=None,
        debug_logging_override=True,
    )

    assert effective.logging.level == "DEBUG"
    candidate = effective.model_copy(
        update={
            "logging": effective.logging.model_copy(
                update={"max_files": effective.logging.max_files + 1}
            )
        }
    )
    persisted = _persistable_config(
        candidate,
        base=base,
        mode_overridden=False,
        data_dir_overridden=False,
        debug_logging_overridden=True,
    )

    assert persisted.logging.level == "INFO"
    assert persisted.logging.max_files == base.logging.max_files + 1


def test_launch_without_mode_is_always_live() -> None:
    args = build_parser().parse_args([])

    assert _mode_from_args(args) == "live"


def test_demo_requires_explicit_command_line_flag() -> None:
    args = build_parser().parse_args(["--demo"])

    assert _mode_from_args(args) == "demo"


def test_hardware_preflight_reports_sanitized_rtlsdr_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import alga_vector.devices as devices

    result = SimpleNamespace(
        successful=True,
        devices=(
            SimpleNamespace(
                description="Blog V4",
                connection="RTLSDR:0",
                serial="must-not-be-printed",
            ),
        ),
        issues=(),
    )
    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: object())
    monkeypatch.setattr(
        devices,
        "RtlSdrDiscoveryService",
        lambda: SimpleNamespace(discover=lambda: result),
    )
    monkeypatch.setattr(
        cli,
        "_optional_receiver_preflight_lines",
        lambda: (
            "HackRF (optional, RX-only): tools absent.",
            "tinySA (optional): metadata-only; COM ports are not opened automatically.",
        ),
    )

    assert cli._hardware_preflight() == 0
    output = capsys.readouterr().out
    assert "Blog V4 (RTLSDR:0)" in output
    assert "must-not-be-printed" not in output
    assert "pyserial=OK" in output
    assert "RX-only" in output
    assert "metadata-only" in output


def test_hardware_preflight_fails_on_discovery_worker_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import alga_vector.devices as devices

    result = SimpleNamespace(
        successful=False,
        devices=(),
        issues=(SimpleNamespace(message_ru="Поиск RTL-SDR недоступен."),),
    )
    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: object())
    monkeypatch.setattr(
        devices,
        "RtlSdrDiscoveryService",
        lambda: SimpleNamespace(discover=lambda: result),
    )

    assert cli._hardware_preflight() == 2
    assert "Поиск RTL-SDR недоступен" in capsys.readouterr().err


def test_hardware_preflight_optional_receivers_can_be_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import alga_vector.devices as devices
    import alga_vector.devices.host_tools as host_tools

    class MissingOptionalTools:
        @staticmethod
        def find(_tool_name: str) -> None:
            return None

    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: object())
    monkeypatch.setattr(
        devices,
        "RtlSdrDiscoveryService",
        lambda: SimpleNamespace(
            discover=lambda: SimpleNamespace(
                successful=True,
                devices=(),
                issues=(),
            )
        ),
    )
    monkeypatch.setattr(
        devices,
        "TinySaSerialDiscoveryService",
        lambda: SimpleNamespace(
            discover=lambda: SimpleNamespace(
                successful=True,
                candidates=(),
                issues=(),
            )
        ),
    )
    monkeypatch.setattr(
        host_tools,
        "SubprocessHostTools",
        MissingOptionalTools,
    )

    assert cli._hardware_preflight() == 0
    output = capsys.readouterr().out
    assert "hackrf_info=absent" in output
    assert "hackrf_transfer=absent" in output
    assert "tinySA (optional)" in output
    assert "COM-порты автоматически не открываются" in output


def test_hardware_preflight_reports_official_hackrf_tools_without_serial(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import alga_vector.devices as devices
    import alga_vector.devices.host_tools as host_tools

    secret_serial = "0000000000000000deadbeefcafebabe"

    class BundledHostTools:
        @staticmethod
        def find(tool_name: str) -> str:
            return f"C:\\ALGA\\hardware-tools\\{tool_name}.exe"

    monkeypatch.setattr(cli.importlib, "import_module", lambda _name: object())
    monkeypatch.setattr(
        devices,
        "RtlSdrDiscoveryService",
        lambda: SimpleNamespace(
            discover=lambda: SimpleNamespace(
                successful=True,
                devices=(),
                issues=(),
            )
        ),
    )
    monkeypatch.setattr(
        devices,
        "HackRfDiscoveryService",
        lambda **_kwargs: SimpleNamespace(
            discover=lambda: SimpleNamespace(
                successful=True,
                devices=(
                    SimpleNamespace(
                        board_name="HackRF One",
                        serial=secret_serial,
                    ),
                ),
                issues=(),
            )
        ),
    )
    monkeypatch.setattr(
        devices,
        "TinySaSerialDiscoveryService",
        lambda: SimpleNamespace(
            discover=lambda: SimpleNamespace(
                successful=True,
                candidates=(
                    SimpleNamespace(
                        description="tinySA Ultra",
                        connection="COM7",
                    ),
                ),
                issues=(),
            )
        ),
    )
    monkeypatch.setattr(
        host_tools,
        "SubprocessHostTools",
        BundledHostTools,
    )

    assert cli._hardware_preflight() == 0
    output = capsys.readouterr().out
    assert "hackrf_info=available (hardware-tools)" in output
    assert "hackrf_transfer=available (hardware-tools)" in output
    assert "RX-only / только приём" in output
    assert "never exposes a TX command" in output
    assert "confirmed receivers: 1 (HackRF One)" in output
    assert secret_serial not in output
    assert "tinySA Ultra (COM7)" in output


def test_hardware_preflight_still_fails_when_required_module_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def import_required(name: str) -> object:
        if name == "rtlsdr":
            raise ImportError("pyrtlsdr missing")
        return object()

    monkeypatch.setattr(cli.importlib, "import_module", import_required)

    assert cli._hardware_preflight() == 2
    assert "pyrtlsdr missing" in capsys.readouterr().err


def test_legacy_demo_profile_cannot_leak_simulators_into_normal_launch(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        mode="demo",
        devices=DevicesConfig(
            enable_real_adapters=True,
            adapters=[
                AdapterConfig(
                    id="legacy-sim",
                    kind="tinysa",
                    enabled=True,
                    connection="SIM:TINYSA",
                ),
                AdapterConfig(
                    id="physical",
                    kind="rtlsdr",
                    enabled=True,
                    connection="RTLSDR:0",
                ),
            ],
        ),
    )

    effective = _apply_runtime_overrides(
        config,
        user_data_dir=tmp_path,
        mode_override=None,
        data_dir_override=None,
    )

    assert effective.mode == "live"
    assert [adapter.id for adapter in effective.devices.adapters] == ["physical"]
    assert effective.devices.enable_real_adapters is False
    assert not has_enabled_real_hardware(effective)


def test_explicit_demo_replaces_physical_topology_with_training_sources(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        devices=DevicesConfig(
            enable_real_adapters=True,
            adapters=[
                AdapterConfig(
                    id="physical",
                    kind="tinysa",
                    enabled=True,
                    connection="COM7",
                )
            ],
        )
    )

    effective = _apply_runtime_overrides(
        config,
        user_data_dir=tmp_path,
        mode_override="demo",
        data_dir_override=None,
    )

    assert effective.mode == "demo"
    assert effective.devices.enable_real_adapters is False
    assert {
        adapter.connection for adapter in effective.devices.adapters
    } == {"SIM:TINYSA", "SIM:RTLSDR"}
    assert not has_enabled_real_hardware(effective)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-relative path semantics")
def test_drive_relative_data_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        _apply_runtime_overrides(
            AppConfig(),
            user_data_dir=tmp_path,
            mode_override=None,
            data_dir_override=Path("D:captures"),
        )
