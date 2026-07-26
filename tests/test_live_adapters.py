from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from alga_vector.devices.live import (
    RtlSdrAdapter,
    TinySASerialAdapter,
    _iq_to_dbfs,
)
from alga_vector.domain.enums import Provenance
from alga_vector.domain.errors import AppError


class FakeSerialPort:
    def __init__(self) -> None:
        self.is_open = True
        self.timeout = 1.0
        self.commands: list[str] = []

    def reset_input_buffer(self) -> None:
        return

    def write(self, payload: bytes) -> None:
        self.commands.append(payload.decode("ascii").strip())

    def flush(self) -> None:
        return

    def read_until(self, _marker: bytes, _maximum: int) -> bytes:
        command = self.commands[-1]
        if command == "version":
            return b"version\r\ntinySA ULTRA test-fw\r\nch>"
        points = int(command.split()[3])
        rows = b"\r\n".join(f"{-100.0 + index / 10:.1f}".encode() for index in range(points))
        return command.encode() + b"\r\n" + rows + b"\r\nch>"

    def close(self) -> None:
        self.is_open = False


class ScanFailingSerialPort(FakeSerialPort):
    def read_until(self, marker: bytes, maximum: int) -> bytes:
        if self.commands[-1].startswith("scan "):
            raise OSError("deterministic serial disconnect")
        return super().read_until(marker, maximum)


def test_tinysa_reads_only_explicit_port_and_returns_dbm() -> None:
    port = FakeSerialPort()
    module = SimpleNamespace(Serial=lambda **_kwargs: port)
    adapter = TinySASerialAdapter(
        adapter_id="tiny-01",
        connection="COM7",
        serial_module=module,
    )

    snapshot = adapter.inspect()
    frame = adapter.read_spectrum(
        sequence=3,
        center_frequency_hz=100_000_000,
        span_hz=2_000_000,
        bins=64,
    )

    assert snapshot.connection == "COM7"
    assert frame.provenance == Provenance.LIVE
    assert frame.unit == "dBm"
    assert frame.calibration_id is None
    assert frame.uncertainty_db is None
    assert frame.power_dbm.dtype == np.float32
    assert len(frame.power_dbm) == 64
    assert port.commands[0] == "version"
    assert port.commands[1].startswith("scan ")


def test_tinysa_io_failure_invalidates_serial_handle_before_next_inspect() -> None:
    ports = [ScanFailingSerialPort(), FakeSerialPort()]

    def factory(**_kwargs: object) -> FakeSerialPort:
        return ports.pop(0)

    first_port = ports[0]
    adapter = TinySASerialAdapter(
        adapter_id="tiny-io-failure",
        connection="COM7",
        serial_module=SimpleNamespace(Serial=factory),
    )
    adapter.inspect()

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=100_000_000,
            span_hz=2_000_000,
            bins=64,
        )

    assert caught.value.code == "DEVICE.TINYSA_IO_FAILED"
    assert not first_port.is_open
    assert adapter.inspect().state.value == "ready"
    assert not ports


def test_tinysa_rejects_non_port_expression() -> None:
    with pytest.raises(ValueError, match="explicit COM port"):
        TinySASerialAdapter(
            adapter_id="tiny-01",
            connection="COM7;SCAN_ALL",
        )


class FakeRtlReceiver:
    def __init__(self, *, device_index: int) -> None:
        self.device_index = device_index
        self.sample_rate = 0
        self.center_freq = 0
        self.gain: str | float = "auto"
        self.direct_sampling_calls: list[int] = []
        self.closed = False

    def read_samples(self, count: int) -> np.ndarray:
        axis = np.arange(count, dtype=np.float64)
        return np.exp(2j * np.pi * axis * 0.125).astype(np.complex64)

    def close(self) -> None:
        self.closed = True

    def set_direct_sampling(self, value: int) -> None:
        self.direct_sampling_calls.append(value)


class TuneFailingRtlReceiver(FakeRtlReceiver):
    def __init__(self, *, device_index: int) -> None:
        self._center_frequency = 0
        self._fail_tuning = False
        super().__init__(device_index=device_index)
        self._fail_tuning = True

    @property
    def center_freq(self) -> int:
        return self._center_frequency

    @center_freq.setter
    def center_freq(self, value: int) -> None:
        if self._fail_tuning:
            raise OSError("deterministic tune failure")
        self._center_frequency = value


class ReadFailingRtlReceiver(FakeRtlReceiver):
    def read_samples(self, count: int) -> np.ndarray:
        del count
        raise OSError("deterministic read failure")


class CountingTuningRtlReceiver:
    def __init__(self, *, device_index: int) -> None:
        self.device_index = device_index
        self.sample_rate_values: list[int] = []
        self.center_frequency_values: list[int] = []
        self.gain_values: list[str | float] = []
        self.direct_sampling_calls: list[int] = []
        self.read_sample_counts: list[int] = []
        self.closed = False
        self._sample_rate = 0
        self._center_frequency = 0
        self._gain: str | float = "auto"

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, value: int) -> None:
        self.sample_rate_values.append(value)
        self._sample_rate = value

    @property
    def center_freq(self) -> int:
        return self._center_frequency

    @center_freq.setter
    def center_freq(self, value: int) -> None:
        self.center_frequency_values.append(value)
        self._center_frequency = value

    @property
    def gain(self) -> str | float:
        return self._gain

    @gain.setter
    def gain(self, value: str | float) -> None:
        self.gain_values.append(value)
        self._gain = value

    def read_samples(self, count: int) -> np.ndarray:
        self.read_sample_counts.append(count)
        axis = np.arange(count, dtype=np.float64)
        return np.exp(2j * np.pi * axis * 0.125).astype(np.complex64)

    def close(self) -> None:
        self.closed = True

    def set_direct_sampling(self, value: int) -> None:
        self.direct_sampling_calls.append(value)


class NonFiniteRtlReceiver(FakeRtlReceiver):
    def read_samples(self, count: int) -> np.ndarray:
        samples = super().read_samples(count)
        samples[count // 2] = np.complex64(complex(float("nan"), 0.0))
        return samples


def test_rtlsdr_computes_live_dbfs_spectrum() -> None:
    created: list[FakeRtlReceiver] = []

    def factory(*, device_index: int) -> FakeRtlReceiver:
        receiver = FakeRtlReceiver(device_index=device_index)
        created.append(receiver)
        return receiver

    adapter = RtlSdrAdapter(
        adapter_id="rtl-01",
        connection="RTLSDR:2",
        sample_rate_hz=2_400_000,
        rtlsdr_module=SimpleNamespace(RtlSdr=factory),
    )

    assert adapter.inspect().metrics["device_index"] == 2
    frame = adapter.read_spectrum(
        sequence=9,
        center_frequency_hz=433_920_000,
        span_hz=1_200_000,
        bins=128,
    )

    assert frame.provenance == Provenance.LIVE
    assert frame.unit == "dBFS"
    assert frame.power_dbm.shape == (128,)
    assert np.all(np.isfinite(frame.power_dbm))
    assert created[0].sample_rate == 2_400_000
    assert created[0].center_freq == 433_920_000
    # The synthetic tone is +300 kHz from centre.  In a ±600 kHz requested
    # span it belongs near 75% of the displayed frequency axis.
    assert 88 <= int(np.argmax(frame.power_dbm)) <= 104


def test_rtlsdr_reuses_identical_tuning_and_discards_only_after_retune() -> None:
    receiver = CountingTuningRtlReceiver(device_index=0)
    adapter = RtlSdrAdapter(
        adapter_id="rtl-tuning-cache",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        rtlsdr_module=SimpleNamespace(RtlSdr=lambda **_kwargs: receiver),
    )

    adapter.read_spectrum(
        sequence=1,
        center_frequency_hz=433_920_000,
        span_hz=1_200_000,
        bins=128,
    )
    adapter.read_spectrum(
        sequence=2,
        center_frequency_hz=433_920_000,
        span_hz=1_200_000,
        bins=128,
    )
    adapter.read_spectrum(
        sequence=3,
        center_frequency_hz=434_100_000,
        span_hz=1_200_000,
        bins=128,
    )

    assert receiver.sample_rate_values == [2_400_000]
    assert receiver.center_frequency_values == [433_920_000, 434_100_000]
    assert receiver.gain_values == ["auto"]
    assert receiver.direct_sampling_calls == [0]
    assert len(receiver.read_sample_counts) == 5
    settling_first, capture_first, capture_second, settling_retune, capture_retune = (
        receiver.read_sample_counts
    )
    assert settling_first == settling_retune
    assert capture_first == capture_second == capture_retune
    assert settling_first != capture_first


def test_robust_welch_spectrum_preserves_persistent_tone() -> None:
    segment_size = 1_024
    sample_count = segment_size * 3
    axis = np.arange(sample_count, dtype=np.float64)
    samples = (
        0.5 * np.exp(2j * np.pi * axis * 0.125)
    ).astype(np.complex64)

    spectrum = _iq_to_dbfs(
        samples,
        128,
        segment_size=segment_size,
    )

    assert spectrum.shape == (128,)
    assert spectrum.dtype == np.float32
    assert np.all(np.isfinite(spectrum))
    assert 76 <= int(np.argmax(spectrum)) <= 84
    assert float(np.max(spectrum)) > -20.0


def test_robust_welch_spectrum_rejects_one_sample_outlier() -> None:
    segment_size = 512
    samples = np.zeros(segment_size * 3, dtype=np.complex64)
    samples[segment_size + segment_size // 3] = np.complex64(1_000_000 + 0j)

    spectrum = _iq_to_dbfs(
        samples,
        64,
        segment_size=segment_size,
    )

    assert spectrum.shape == (64,)
    assert np.all(np.isfinite(spectrum))
    assert float(np.max(spectrum)) <= -200.0


def test_nonfinite_iq_is_fail_closed_and_invalidates_receiver() -> None:
    receiver = NonFiniteRtlReceiver(device_index=0)
    adapter = RtlSdrAdapter(
        adapter_id="rtl-nonfinite",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        rtlsdr_module=SimpleNamespace(RtlSdr=lambda **_kwargs: receiver),
    )

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=433_920_000,
            span_hz=1_200_000,
            bins=64,
        )

    assert caught.value.code == "SPECTRUM.NONFINITE_IQ"
    assert receiver.closed


@pytest.mark.parametrize(
    ("receiver_type", "expected_code"),
    [
        (TuneFailingRtlReceiver, "SPECTRUM.RTLSDR_TUNE_FAILED"),
        (ReadFailingRtlReceiver, "DEVICE.RTLSDR_READ_FAILED"),
    ],
)
def test_rtlsdr_tune_or_read_failure_invalidates_receiver(
    receiver_type: type[FakeRtlReceiver],
    expected_code: str,
) -> None:
    created: list[FakeRtlReceiver] = []

    def factory(*, device_index: int) -> FakeRtlReceiver:
        receiver = receiver_type(device_index=device_index)
        created.append(receiver)
        return receiver

    adapter = RtlSdrAdapter(
        adapter_id="rtl-failure",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        rtlsdr_module=SimpleNamespace(RtlSdr=factory),
    )
    adapter.inspect()

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=433_920_000,
            span_hz=1_200_000,
            bins=64,
        )

    assert caught.value.code == expected_code
    assert created[0].closed
    assert adapter.inspect().state.value == "ready"
    assert len(created) == 2


def test_driver_confirmed_blog_v4_uses_upconverter_without_direct_sampling() -> None:
    created: list[FakeRtlReceiver] = []

    def factory(*, device_index: int) -> FakeRtlReceiver:
        receiver = FakeRtlReceiver(device_index=device_index)
        created.append(receiver)
        return receiver

    adapter = RtlSdrAdapter(
        adapter_id="rtl-v4",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        profile_override="blog_v4",
        rtlsdr_module=SimpleNamespace(
            RtlSdr=factory,
            usb_identity=("RTLSDRBlog", "Blog V4"),
        ),
    )

    snapshot = adapter.inspect()
    frame = adapter.read_spectrum(
        sequence=1,
        center_frequency_hz=10_000_000,
        span_hz=2_000_000,
        bins=64,
    )

    assert snapshot.metrics["detected_tuning_profile_id"] == "rtlsdr_blog_v4"
    assert snapshot.metrics["tuning_profile_id"] == "rtlsdr_blog_v4"
    assert snapshot.metrics["profile_selection"] == "operator_confirmed"
    assert frame.center_frequency_hz == 10_000_000
    assert created[0].direct_sampling_calls == [0]


def test_unconfirmed_blog_v4_override_falls_back_and_blocks_hf() -> None:
    adapter = RtlSdrAdapter(
        adapter_id="rtl-v4-unconfirmed",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        profile_override="blog_v4",
        rtlsdr_module=SimpleNamespace(
            RtlSdr=FakeRtlReceiver,
            usb_identity=("Generic", "RTL2832U OEM"),
        ),
    )

    snapshot = adapter.inspect()
    assert snapshot.metrics["detected_tuning_profile_id"] == "generic_r820t"
    assert snapshot.metrics["tuning_profile_id"] == "generic_r820t"
    assert snapshot.metrics["profile_selection"] == "operator_unconfirmed_fallback"
    assert "HF отключён" in str(snapshot.metrics["profile_warning_ru"])

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=10_000_000,
            span_hz=2_000_000,
            bins=64,
        )
    assert caught.value.code == "SPECTRUM.FREQUENCY_OUTSIDE_DEVICE_RANGE"


def test_generic_auto_profile_rejects_unverified_hf() -> None:
    adapter = RtlSdrAdapter(
        adapter_id="rtl-generic",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        rtlsdr_module=SimpleNamespace(
            RtlSdr=FakeRtlReceiver,
            usb_identity=("Generic", "RTL2832U OEM"),
        ),
    )

    with pytest.raises(AppError) as caught:
        adapter.read_spectrum(
            sequence=1,
            center_frequency_hz=10_000_000,
            span_hz=2_000_000,
            bins=64,
        )

    assert getattr(caught.value, "code", "") == (
        "SPECTRUM.FREQUENCY_OUTSIDE_DEVICE_RANGE"
    )


def test_blog_v3_operator_profile_switches_q_direct_sampling_by_band() -> None:
    created: list[FakeRtlReceiver] = []

    def factory(*, device_index: int) -> FakeRtlReceiver:
        receiver = FakeRtlReceiver(device_index=device_index)
        created.append(receiver)
        return receiver

    adapter = RtlSdrAdapter(
        adapter_id="rtl-v3",
        connection="RTLSDR:0",
        sample_rate_hz=2_400_000,
        profile_override="blog_v3_direct_q",
        rtlsdr_module=SimpleNamespace(
            RtlSdr=factory,
            usb_identity=("Generic", "RTL2832U OEM"),
        ),
    )
    adapter.read_spectrum(
        sequence=1,
        center_frequency_hz=10_000_000,
        span_hz=2_000_000,
        bins=64,
    )
    adapter.read_spectrum(
        sequence=2,
        center_frequency_hz=100_000_000,
        span_hz=2_000_000,
        bins=64,
    )

    assert created[0].direct_sampling_calls == [2, 0]
