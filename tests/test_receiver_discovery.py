from __future__ import annotations

from dataclasses import dataclass

from alga_vector.devices.host_tools import (
    HostCommandResult,
    HostToolTimedOut,
)
from alga_vector.devices.receiver_discovery import (
    HackRfDiscoveryService,
    ReceiverDiscoveryState,
    SerialCandidateConfidence,
    TinySaSerialDiscoveryService,
)


class FakeHostTools:
    def __init__(
        self,
        *,
        info_results: list[HostCommandResult | Exception] | None = None,
        available: bool = True,
    ) -> None:
        self.info_results = list(info_results or [])
        self.available = available
        self.calls: list[tuple[str, ...]] = []

    def find(self, tool_name: str) -> str | None:
        return f"C:\\safe-tools\\{tool_name}.exe" if self.available else None

    def run(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
        maximum_stdout_bytes: int,
    ) -> HostCommandResult:
        del timeout_seconds, maximum_stdout_bytes
        self.calls.append(command)
        result = self.info_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_hackrf_discovery_parses_only_confirmed_usb_mode_devices() -> None:
    payload = b"""
hackrf_info version: 2026.01.3
Found HackRF
Index: 0
Serial number: 0000000000000000deadbeefcafebabe
Board ID Number: 2 (HackRF One)
Firmware Version: 2026.01.3 (API:1.09)
"""
    tools = FakeHostTools(
        info_results=[HostCommandResult(0, payload, b"")]
    )

    result = HackRfDiscoveryService(host_tools=tools).discover()

    assert result.state == ReceiverDiscoveryState.COMPLETE
    assert result.devices[0].connection == (
        "HACKRF:0000000000000000deadbeefcafebabe"
    )
    assert result.devices[0].board_name == "HackRF One"
    assert tools.calls == [("C:\\safe-tools\\hackrf_info.exe",)]


def test_portapack_outside_hackrf_usb_mode_is_reported_as_empty() -> None:
    tools = FakeHostTools(
        info_results=[
            HostCommandResult(0, b"No HackRF boards found.\n", b"")
        ]
    )

    result = HackRfDiscoveryService(host_tools=tools).discover()

    assert result.state == ReceiverDiscoveryState.EMPTY
    assert not result.devices


def test_hackrf_discovery_retries_bounded_timeout_and_missing_tool_is_graceful() -> None:
    timed_out = FakeHostTools(
        info_results=[
            HostToolTimedOut("first"),
            HostToolTimedOut("second"),
        ]
    )
    timeout_result = HackRfDiscoveryService(
        host_tools=timed_out,
        attempts=2,
    ).discover()
    unavailable = HackRfDiscoveryService(
        host_tools=FakeHostTools(available=False)
    ).discover()

    assert timeout_result.state == ReceiverDiscoveryState.TIMED_OUT
    assert len(timed_out.calls) == 2
    assert unavailable.state == ReceiverDiscoveryState.UNAVAILABLE
    assert unavailable.issues[0].code == "DEVICE.HACKRF_INFO_MISSING"


@dataclass
class PortMetadata:
    device: str
    description: str
    manufacturer: str | None = None
    product: str | None = None
    interface: str | None = None
    vid: int | None = None
    pid: int | None = None


def test_tinysa_discovery_uses_metadata_only_and_marks_ambiguous_usb_serial() -> None:
    ports = [
        PortMetadata(
            device="COM7",
            description="tinySA Ultra USB Serial",
            manufacturer="tinySA",
            product="tinySA4",
            vid=0x0483,
            pid=0x5740,
        ),
        PortMetadata(
            device="COM8",
            description="USB Serial Device",
            manufacturer="Generic",
            vid=0x1234,
            pid=0x5678,
        ),
        PortMetadata(
            device="COM1",
            description="Communications Port",
        ),
    ]
    provider_calls = 0

    def provider() -> list[PortMetadata]:
        nonlocal provider_calls
        provider_calls += 1
        return ports

    result = TinySaSerialDiscoveryService(port_provider=provider).discover()

    assert provider_calls == 1
    assert result.scanned_port_count == 3
    assert [item.connection for item in result.candidates] == ["COM7", "COM8"]
    assert (
        result.candidates[0].confidence
        == SerialCandidateConfidence.CONFIRMED_METADATA
    )
    assert (
        result.candidates[1].confidence
        == SerialCandidateConfidence.POSSIBLE_USB_SERIAL
    )
    assert result.state == ReceiverDiscoveryState.PARTIAL
    assert result.issues[0].code == "DEVICE.TINYSA_CONFIRMATION_REQUIRED"
