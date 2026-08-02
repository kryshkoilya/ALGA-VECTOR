from __future__ import annotations

import numpy as np
import pytest

from alga_vector.devices.hackrf import HackRfReceiveAdapter
from alga_vector.devices.host_tools import HostCommandResult, HostToolTimedOut
from alga_vector.domain.enums import Provenance
from alga_vector.domain.errors import AppError

_SERIAL = "0000000000000000deadbeefcafebabe"
_INFO = f"""
Found HackRF
Index: 0
Serial number: {_SERIAL}
Board ID Number: 2 (HackRF One)
Firmware Version: test-fw
""".encode()


class AdapterHostTools:
    def __init__(self, *, capture_timeout: bool = False) -> None:
        self.capture_timeout = capture_timeout
        self.calls: list[tuple[str, ...]] = []

    def find(self, tool_name: str) -> str | None:
        return f"C:\\safe-tools\\{tool_name}.exe"

    def run(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
        maximum_stdout_bytes: int,
    ) -> HostCommandResult:
        del timeout_seconds
        self.calls.append(command)
        if command[0].endswith("hackrf_info.exe"):
            return HostCommandResult(0, _INFO, b"")
        if self.capture_timeout:
            raise HostToolTimedOut("deterministic timeout")
        count = int(command[command.index("-n") + 1])
        axis = np.arange(count, dtype=np.float64)
        i_values = np.asarray(
            np.round(60.0 * np.cos(2.0 * np.pi * axis / 32.0)),
            dtype=np.int8,
        )
        q_values = np.asarray(
            np.round(60.0 * np.sin(2.0 * np.pi * axis / 32.0)),
            dtype=np.int8,
        )
        interleaved = np.empty(count * 2, dtype=np.int8)
        interleaved[0::2] = i_values
        interleaved[1::2] = q_values
        payload = interleaved.tobytes()
        assert len(payload) == maximum_stdout_bytes
        return HostCommandResult(0, payload, b"")


def test_hackrf_adapter_uses_receive_only_command_and_returns_live_dbfs() -> None:
    tools = AdapterHostTools()
    adapter = HackRfReceiveAdapter(
        adapter_id="hackrf-01",
        connection=f"HACKRF:{_SERIAL}",
        sample_rate_hz=2_000_000,
        host_tools=tools,
    )

    snapshot = adapter.inspect()
    frame = adapter.read_spectrum(
        sequence=7,
        center_frequency_hz=100_000_000,
        span_hz=2_000_000,
        bins=64,
    )

    assert snapshot.metrics["receive_only"] == 1
    assert snapshot.last_data_at is None
    assert snapshot.metrics["rf_amp_enabled"] == 0
    assert snapshot.metrics["lna_gain_db"] == 16
    assert snapshot.metrics["vga_gain_db"] == 20
    assert frame.provenance == Provenance.LIVE
    assert frame.unit == "dBFS"
    assert frame.power_dbm.shape == (64,)
    transfer = next(
        command for command in tools.calls if command[0].endswith("hackrf_transfer.exe")
    )
    assert "-r" in transfer
    assert "-t" not in transfer
    assert "-c" not in transfer
    assert transfer[transfer.index("-a") + 1] == "0"
    assert transfer[transfer.index("-p") + 1] == "0"
    assert transfer[transfer.index("-d") + 1] == _SERIAL


def test_hackrf_adapter_rejects_unsupported_window_before_capture() -> None:
    tools = AdapterHostTools()
    adapter = HackRfReceiveAdapter(
        adapter_id="hackrf-edge",
        connection=f"HACKRF:{_SERIAL}",
        sample_rate_hz=2_000_000,
        host_tools=tools,
    )

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=1_000_000,
            span_hz=2_000_000,
            bins=64,
        )

    assert caught.value.code == "SPECTRUM.WINDOW_OUTSIDE_DEVICE_RANGE"
    assert not tools.calls


def test_hackrf_capture_timeout_retries_twice_then_fails_closed() -> None:
    tools = AdapterHostTools(capture_timeout=True)
    adapter = HackRfReceiveAdapter(
        adapter_id="hackrf-timeout",
        connection=f"HACKRF:{_SERIAL}",
        sample_rate_hz=2_000_000,
        host_tools=tools,
    )

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=100_000_000,
            span_hz=2_000_000,
            bins=64,
        )

    assert caught.value.code == "DEVICE.HACKRF_CAPTURE_TIMEOUT"
    transfer_calls = [
        command
        for command in tools.calls
        if command[0].endswith("hackrf_transfer.exe")
    ]
    assert len(transfer_calls) == 2
