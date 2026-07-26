from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from alga_vector.application.runtime import ApplicationRuntime
from alga_vector.config import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    MapConfig,
    StorageConfig,
)
from alga_vector.devices import (
    DeviceManager,
    PyRtlSdrEnumerator,
    RtlSdrDiscoveredDevice,
    RtlSdrDiscoveryIssue,
    RtlSdrDiscoveryResult,
    RtlSdrDiscoveryService,
    RtlSdrDiscoveryState,
    RtlSdrEnumeration,
    WindowsPnpDeviceStatus,
    WindowsRtlSdrPnpDiagnostic,
)
from alga_vector.domain.errors import AppError


class FakeEnumerator:
    def __init__(
        self,
        devices: tuple[RtlSdrDiscoveredDevice, ...] = (),
        *,
        failure: Exception | None = None,
    ) -> None:
        self.devices = devices
        self.failure = failure
        self.calls: list[int] = []

    def enumerate_devices(self, *, max_devices: int) -> RtlSdrEnumeration:
        self.calls.append(max_devices)
        if self.failure is not None:
            raise self.failure
        return RtlSdrEnumeration(
            devices=self.devices,
            reported_count=len(self.devices),
        )


def _candidate(index: int = 0) -> RtlSdrDiscoveredDevice:
    return RtlSdrDiscoveredDevice(
        index=index,
        connection=f"RTLSDR:{index}",
        description="RTL-SDR Blog V4",
        serial=f"SERIAL-{index}",
        manufacturer="RTLSDRBlog",
    )


def _config(
    data_dir: Path,
    *,
    mode: str = "live",
    adapters: list[AdapterConfig] | None = None,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "mode": mode,
            "storage": StorageConfig(data_dir=data_dir).model_dump(mode="python"),
            "map": MapConfig(network_enabled=False).model_dump(mode="python"),
            "devices": DevicesConfig(
                enable_real_adapters=False,
                adapters=adapters or [],
            ).model_dump(mode="python"),
        }
    )


def test_injected_enumerator_returns_detached_descriptors_without_activation() -> None:
    candidate = _candidate()
    enumerator = FakeEnumerator((candidate,))
    service = RtlSdrDiscoveryService(enumerator=enumerator, max_devices=4)

    result = service.discover()

    assert result.state == RtlSdrDiscoveryState.COMPLETE
    assert result.successful is True
    assert result.devices == (candidate,)
    assert result.scanned_count == 1
    assert enumerator.calls == [4]


def test_discovery_failure_is_structured_and_does_not_expose_exception_text() -> None:
    enumerator = FakeEnumerator(failure=OSError("secret native backend detail"))
    service = RtlSdrDiscoveryService(enumerator=enumerator)

    result = service.discover()

    assert result.state == RtlSdrDiscoveryState.UNAVAILABLE
    assert result.devices == ()
    assert result.issues[0].code == "DEVICE.RTLSDR_BACKEND_UNAVAILABLE"
    assert "secret" not in result.issues[0].message_ru


def test_empty_librtlsdr_result_reports_windows_code_28_first() -> None:
    pnp = WindowsRtlSdrPnpDiagnostic(
        device_reader=lambda: (
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_0BDA&PID_2838&REV_0100",),
                description="RTL2832U",
                service=None,
                driver_key=None,
                problem_code=28,
            ),
        )
    )
    service = RtlSdrDiscoveryService(
        enumerator=FakeEnumerator(),
        pnp_diagnostic=pnp,
    )

    result = service.discover()

    assert result.state == RtlSdrDiscoveryState.UNAVAILABLE
    assert result.successful is False
    assert result.issues[0].code == "DEVICE.RTLSDR_WINDOWS_DRIVER_CODE_28"
    assert "код диспетчера устройств 28" in result.issues[0].message_ru
    assert "WinUSB" in result.issues[0].operator_action_ru


def test_windows_pnp_reports_wrong_driver_binding_without_claiming_receiver_found() -> None:
    pnp = WindowsRtlSdrPnpDiagnostic(
        device_reader=lambda: (
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_0BDA&PID_2838",),
                description="RTL-SDR Blog V4",
                service="RTL2832UUSB",
                driver_key="{driver-class}\\0001",
                problem_code=0,
            ),
        )
    )

    issue = pnp.diagnose_attached_receiver()

    assert issue is not None
    assert issue.code == "DEVICE.RTLSDR_WINDOWS_DRIVER_NOT_WINUSB"
    assert "RTL2832UUSB" in issue.message_ru
    assert "этой сборке нужен WinUSB" in issue.message_ru


def test_windows_pnp_reports_backend_hidden_when_winusb_binding_is_healthy() -> None:
    pnp = WindowsRtlSdrPnpDiagnostic(
        device_reader=lambda: (
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_0BDA&PID_2838",),
                description="RTL-SDR Blog V4",
                service="WinUSB",
                driver_key="{88bae032-5a81-49f0-bc3d-a4ff138216d6}\\0001",
                problem_code=0,
            ),
        )
    )

    issue = pnp.diagnose_attached_receiver()

    assert issue is not None
    assert issue.code == "DEVICE.RTLSDR_WINDOWS_BACKEND_HIDDEN"
    assert "librtlsdr не получил устройство" in issue.message_ru
    assert "другие SDR-программы" in issue.operator_action_ru


def test_windows_pnp_prefers_working_mi_00_over_auxiliary_code_28() -> None:
    pnp = WindowsRtlSdrPnpDiagnostic(
        device_reader=lambda: (
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_0BDA&PID_2838&MI_00",),
                description="Bulk-In, Interface",
                service="WinUSB",
                driver_key="{driver-class}\\0001",
                problem_code=0,
            ),
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_0BDA&PID_2838&MI_01",),
                description="Bulk-In, Interface",
                service=None,
                driver_key=None,
                problem_code=28,
            ),
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_0BDA&PID_2838",),
                description="USB Composite Device",
                service="usbccgp",
                driver_key="{composite-class}\\0001",
                problem_code=0,
            ),
        )
    )

    issue = pnp.diagnose_attached_receiver()

    assert issue is not None
    assert issue.code == "DEVICE.RTLSDR_WINDOWS_BACKEND_HIDDEN"


def test_windows_pnp_ignores_unrelated_usb_device() -> None:
    pnp = WindowsRtlSdrPnpDiagnostic(
        device_reader=lambda: (
            WindowsPnpDeviceStatus(
                hardware_ids=("USB\\VID_046D&PID_C534",),
                description="USB Receiver",
                service=None,
                driver_key=None,
                problem_code=28,
            ),
        )
    )

    assert pnp.diagnose_attached_receiver() is None


def test_isolated_timeout_keeps_timeout_as_secondary_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic_issue = RtlSdrDiscoveryIssue(
        code="DEVICE.RTLSDR_WINDOWS_DRIVER_CODE_28",
        message_ru="Windows видит RTL2832U: драйвер не установлен (код 28).",
        operator_action_ru="Установите WinUSB.",
        retryable=True,
    )

    class FakePnpDiagnostic:
        def diagnose_attached_receiver(self) -> RtlSdrDiscoveryIssue:
            return diagnostic_issue

    timeout_result = RtlSdrDiscoveryResult(
        state=RtlSdrDiscoveryState.TIMED_OUT,
        devices=(),
        reported_count=0,
        scanned_count=0,
        issues=(
            RtlSdrDiscoveryIssue(
                code="DEVICE.RTLSDR_DISCOVERY_TIMEOUT",
                message_ru="Поиск превысил лимит времени.",
                operator_action_ru="Повторите поиск.",
                retryable=True,
            ),
        ),
    )
    service = RtlSdrDiscoveryService(pnp_diagnostic=FakePnpDiagnostic())
    monkeypatch.setattr(service, "_discover_isolated", lambda: timeout_result)

    result = service.discover()

    assert result.state == RtlSdrDiscoveryState.TIMED_OUT
    assert [issue.code for issue in result.issues] == [
        "DEVICE.RTLSDR_WINDOWS_DRIVER_CODE_28",
        "DEVICE.RTLSDR_DISCOVERY_TIMEOUT",
    ]


class FakeLibRtlSdr:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def rtlsdr_get_device_count(self) -> int:
        self.calls.append(("count", None))
        return 1

    def rtlsdr_get_device_name(self, index: int) -> bytes:
        self.calls.append(("name", index))
        return b"Generic RTL2832U"

    def rtlsdr_get_device_usb_strings(
        self,
        index: int,
        manufacturer: object,
        product: object,
        serial: object,
    ) -> int:
        self.calls.append(("usb_strings", index))
        _fill_buffer(manufacturer, b"RTLSDRBlog")
        _fill_buffer(product, b"Blog V4")
        _fill_buffer(serial, b"00000001")
        return 0


def _fill_buffer(buffer: object, payload: bytes) -> None:
    target = cast(object, buffer)
    for index, value in enumerate(payload):
        target[index] = value  # type: ignore[index]


def test_pyrtlsdr_enumerator_uses_descriptors_without_constructing_receiver() -> None:
    library = FakeLibRtlSdr()
    module = ModuleType("rtlsdr")
    module.librtlsdr = library  # type: ignore[attr-defined]

    def forbidden_receiver(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("discovery must never open an RTL-SDR")

    module.RtlSdr = forbidden_receiver  # type: ignore[attr-defined]
    result = PyRtlSdrEnumerator(module).enumerate_devices(max_devices=4)

    assert result.devices == (
        RtlSdrDiscoveredDevice(
            index=0,
            connection="RTLSDR:0",
            description="Blog V4",
            serial="00000001",
            manufacturer="RTLSDRBlog",
        ),
    )
    assert library.calls == [
        ("count", None),
        ("name", 0),
        ("usb_strings", 0),
    ]


def test_add_discovered_device_is_idempotent_and_collision_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enumerator = FakeEnumerator((_candidate(),))
    service = RtlSdrDiscoveryService(enumerator=enumerator)
    saved: list[AppConfig] = []
    existing = AdapterConfig(
        id="rtl-auto-0",
        kind="rtlsdr",
        enabled=False,
        connection="RTLSDR:3",
    )
    runtime = ApplicationRuntime(
        _config(tmp_path, adapters=[existing]),
        rtl_discovery_service=service,
        config_saver=saved.append,
        background_acquisition=False,
    )
    initial = runtime.start()
    monkeypatch.setattr(
        "alga_vector.application.runtime.build_device_manager",
        lambda *_args, **_kwargs: DeviceManager(()),
    )
    monkeypatch.setattr(runtime, "snapshot", lambda: initial)

    first = runtime.add_discovered_rtlsdr_device("rtlsdr:0")
    second = runtime.add_discovered_rtlsdr_device("RTLSDR:0")

    assert first is initial
    assert second is initial
    assert runtime.config.devices.enable_real_adapters is True
    assert [
        (adapter.id, adapter.connection, adapter.enabled)
        for adapter in runtime.config.devices.adapters
    ] == [
        ("rtl-auto-0", "RTLSDR:3", False),
        ("rtl-auto-0-2", "RTLSDR:0", True),
    ]
    assert len(saved) == 1
    assert enumerator.calls == [16, 16]
    runtime.shutdown()


def test_add_discovered_device_reuses_manual_adapter_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manual = AdapterConfig(
        id="my-v4",
        kind="rtlsdr",
        enabled=False,
        connection="RTLSDR:0",
    )
    runtime = ApplicationRuntime(
        _config(tmp_path, adapters=[manual]),
        rtl_discovery_service=RtlSdrDiscoveryService(
            enumerator=FakeEnumerator((_candidate(),))
        ),
        background_acquisition=False,
    )
    initial = runtime.start()
    monkeypatch.setattr(
        "alga_vector.application.runtime.build_device_manager",
        lambda *_args, **_kwargs: DeviceManager(()),
    )
    monkeypatch.setattr(runtime, "snapshot", lambda: initial)

    runtime.add_discovered_rtlsdr_device("RTLSDR:0")

    assert [
        (adapter.id, adapter.connection, adapter.enabled)
        for adapter in runtime.config.devices.adapters
    ] == [("my-v4", "RTLSDR:0", True)]
    runtime.shutdown()


def test_add_discovered_device_rejects_unknown_connection_without_change(
    tmp_path: Path,
) -> None:
    enumerator = FakeEnumerator((_candidate(),))
    runtime = ApplicationRuntime(
        _config(tmp_path),
        rtl_discovery_service=RtlSdrDiscoveryService(enumerator=enumerator),
        background_acquisition=False,
    )
    before = runtime.config

    with pytest.raises(AppError) as raised:
        runtime.add_discovered_rtlsdr_device("RTLSDR:2")

    assert raised.value.code == "DEVICE.RTLSDR_NOT_DISCOVERED"
    assert runtime.config is before
    assert runtime.config.devices.enable_real_adapters is False
    runtime.shutdown()


@pytest.mark.parametrize("mode", ["safe", "demo"])
def test_add_discovered_device_is_blocked_outside_live_mode(
    tmp_path: Path,
    mode: str,
) -> None:
    enumerator = FakeEnumerator((_candidate(),))
    runtime = ApplicationRuntime(
        _config(tmp_path / mode, mode=mode),
        rtl_discovery_service=RtlSdrDiscoveryService(enumerator=enumerator),
        background_acquisition=False,
    )

    with pytest.raises(AppError) as raised:
        runtime.add_discovered_rtlsdr_device("RTLSDR:0")

    assert raised.value.code == "DEVICE.HARDWARE_MODE_BLOCKED"
    assert runtime.config.devices.enable_real_adapters is False
    assert enumerator.calls == []
    runtime.shutdown()


def test_runtime_discovery_does_not_start_or_mutate_manual_adapter(
    tmp_path: Path,
) -> None:
    manual = AdapterConfig(
        id="manual-rtl",
        kind="rtlsdr",
        enabled=False,
        connection="RTLSDR:0",
    )
    enumerator = FakeEnumerator((_candidate(),))
    runtime = ApplicationRuntime(
        _config(tmp_path, adapters=[manual]),
        rtl_discovery_service=RtlSdrDiscoveryService(enumerator=enumerator),
        background_acquisition=False,
    )
    before = runtime.config.model_dump(mode="python")

    result = runtime.discover_rtlsdr_devices()

    assert result.devices[0].connection == "RTLSDR:0"
    assert runtime.config.model_dump(mode="python") == before
    assert runtime.state.value == "new"
    runtime.shutdown()
