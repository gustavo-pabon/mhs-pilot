import pytest

from bookoo_themis_ultra_mcp import protocol
from bookoo_themis_ultra_mcp.protocol import AutoModeEvent, PowderWeight, ScaleData

# Packets captured from a Themis Ultra with 20.10 g on it.
REAL_SCALE_DATA = "03 0b 00 00 00 01 2b 00 07 da 2b 00 05 50 00 96 01 00 00 16"
REAL_SCALE_DATA_NEGATIVE_FLOW = "03 0b 00 00 00 01 2b 00 07 da 2d 00 01 50 00 96 01 00 00 14"

# Packets built from the protocol tables, with checksums computed by hand.
SCALE_DATA = "03 0b 00 30 39 02 2d 00 01 f4 2b 01 2c 64 01 2c 03 01 00 96"
AUTO_MODE_EVENT = "03 0d 00 00 75 30 2b 00 0e 10 2b 00 c8 00 00 00 00 00 00 9d"
POWDER_WEIGHT = "03 0f 2b 00 07 3a 00 00 00 00 00 00 00 00 00 00 00 00 00 1a"


def parse(packet: str) -> protocol.Packet | None:
    return protocol.parse_packet(bytes.fromhex(packet))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (protocol.tare(), "03 0a 01 00 00 08"),
        (protocol.set_beep_level(2), "03 0a 02 00 02 09"),
        (protocol.set_auto_off_time(15), "03 0a 03 00 0f 05"),
        (protocol.start_timer(), "03 0a 04 00 00 0d"),
        (protocol.stop_timer(), "03 0a 05 00 00 0c"),
        (protocol.reset_timer(), "03 0a 06 00 00 0f"),
        (protocol.tare_and_start_timer(), "03 0a 07 00 00 0e"),
        (protocol.set_flow_smoothing(True), "03 0a 08 01 00 00"),
        (protocol.set_flow_smoothing(False), "03 0a 08 00 00 01"),
        (protocol.calibrate(), "03 0a 09 00 00 00"),
        (protocol.set_auto_stop_condition("ratio", "server_removed"), "03 0a 0b 02 02 02"),
        (protocol.set_auto_stop_condition("timing", "flow_stopped"), "03 0a 0b 01 00 03"),
        (protocol.set_auto_stop_condition_legacy("container_removed"), "03 0a 0b 01 00 03"),
        (protocol.set_powder_weight(0.1), "03 0a 0d 00 01 05"),
        (protocol.set_powder_weight(18.5), "03 0a 0d 00 b9 bd"),
        (protocol.set_powder_weight(999.9), "03 0a 0d 27 0f 2c"),
        (protocol.shut_down(), "03 0a 15 00 00 1c"),
        (protocol.reset_auto_off_countdown(), "03 0a 25 00 00 2c"),
    ],
)
def test_command_bytes(command: bytes, expected: str) -> None:
    assert command.hex(" ") == expected


@pytest.mark.parametrize(
    "build",
    [
        lambda: protocol.set_beep_level(4),
        lambda: protocol.set_auto_off_time(4),
        lambda: protocol.set_auto_off_time(31),
        lambda: protocol.set_powder_weight(0.04),
        lambda: protocol.set_powder_weight(1000),
        lambda: protocol.set_auto_stop_condition("manual", "flow_stopped"),  # type: ignore[arg-type]
        lambda: protocol.set_auto_stop_condition_legacy("server_removed"),  # type: ignore[arg-type]
    ],
)
def test_rejects_values_outside_the_protocol(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_parses_real_scale_data() -> None:
    assert parse(REAL_SCALE_DATA) == ScaleData(
        weight_g=20.1,
        flow_rate_g_per_s=0.05,
        timer_ms=0,
        battery_percent=80,
        display_unit="g",
        auto_off_time_min=15.0,
        beep_level=1,
        flow_smoothing=False,
    )
    assert parse(REAL_SCALE_DATA_NEGATIVE_FLOW).flow_rate_g_per_s == -0.01


def test_parses_scale_data() -> None:
    assert parse(SCALE_DATA) == ScaleData(
        weight_g=-5.0,
        flow_rate_g_per_s=3.0,
        timer_ms=12345,
        battery_percent=100,
        display_unit="oz",
        auto_off_time_min=30.0,
        beep_level=3,
        flow_smoothing=True,
    )


def test_parses_auto_mode_event() -> None:
    assert parse(AUTO_MODE_EVENT) == AutoModeEvent(event="stopped", timer_ms=30000, weight_g=36.0, result=2.0)


def test_parses_powder_weight() -> None:
    assert parse(POWDER_WEIGHT) == PowderWeight(weight_g=18.5)


def test_rejects_corrupted_packets() -> None:
    with pytest.raises(ValueError):
        parse(REAL_SCALE_DATA[:-2] + "17")  # wrong checksum
    with pytest.raises(ValueError):
        parse(REAL_SCALE_DATA[:-3])  # truncated


@pytest.mark.parametrize("packet", ["", "03", "03 0c 00 00 0f", "04 0b 00 00 0f"])
def test_ignores_undocumented_packets(packet: str) -> None:
    assert parse(packet) is None
