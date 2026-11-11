"""BOOKOO Themis Ultra Bluetooth protocol: command builders and packet parsers.

Pure functions with no Bluetooth dependency, implementing BOOKOO's official protocol:
https://github.com/BooKooCode/OpenSource/blob/main/bookoo_ultra_scale/protocols.md

Every packet starts with the product number ``0x03`` and a packet type, and ends with
an XOR checksum of all the bytes before it. Commands are 6 bytes long and are written
to the command characteristic. The scale sends 20-byte data packets as notifications,
decoded here into immutable pydantic models whose field descriptions also document
the MCP tools' output.
"""

from functools import reduce
from operator import xor
from typing import Annotated, Literal

from pydantic import BaseModel, Field

SERVICE_UUID = "00000ffe-0000-1000-8000-00805f9b34fb"
WEIGHT_CHAR_UUID = "0000ff11-0000-1000-8000-00805f9b34fb"
COMMAND_CHAR_UUID = "0000ff12-0000-1000-8000-00805f9b34fb"

PRODUCT_NUMBER = 0x03
COMMAND = 0x0A
SCALE_DATA = 0x0B
AUTO_MODE_EVENT = 0x0D
POWDER_WEIGHT = 0x0F
DATA_PACKET_LENGTH = 20

AutoMode = Literal["timing", "ratio"]
StopCondition = Literal["flow_stopped", "filter_removed", "server_removed"]
LegacyStopCondition = Literal["flow_stopped", "container_removed"]

_AUTO_MODES = {"timing": 0x01, "ratio": 0x02}
_STOP_CONDITIONS = {"flow_stopped": 0x00, "filter_removed": 0x01, "server_removed": 0x02}
_LEGACY_STOP_CONDITIONS = {"flow_stopped": 0x00, "container_removed": 0x01}
_EVENTS = {0x00: "stopped", 0x01: "started", 0x02: "ready", 0x03: "exit_ready", 0x04: "exit_done"}
_UNITS = {0x01: "g", 0x02: "oz"}


def checksum(data: bytes) -> int:
    """Return the protocol checksum of ``data``: the XOR of all its bytes."""
    return reduce(xor, data, 0)


# Commands (packet type 0x0A). Each function returns the 6 bytes to write to
# COMMAND_CHAR_UUID. The scale does not acknowledge commands.


def tare() -> bytes:
    """Zero the scale. Not available while charging or while automatic mode is running or done."""
    return _command(0x01)


def set_beep_level(level: int) -> bytes:
    """Set the beep volume: 0 = mute, 1 = low, 2 = medium, 3 = high."""
    return _command(0x02, 0, _in_range("level", level, 0, 3))


def set_auto_off_time(minutes: int) -> bytes:
    """Set the idle time, 5-30 minutes, after which the scale turns itself off."""
    return _command(0x03, 0, _in_range("minutes", minutes, 5, 30))


def start_timer() -> bytes:
    """Start the timer. Only works in timing and ratio modes."""
    return _command(0x04)


def stop_timer() -> bytes:
    """Stop the timer. Only works in timing and ratio modes."""
    return _command(0x05)


def reset_timer() -> bytes:
    """Reset the timer. Only works in timing and ratio modes."""
    return _command(0x06)


def tare_and_start_timer() -> bytes:
    """Tare, reset and start the timer; in automatic mode, start extraction when ready."""
    return _command(0x07)


def set_flow_smoothing(enabled: bool) -> bytes:
    """Turn flow-rate smoothing on or off."""
    return _command(0x08, int(enabled))


def calibrate() -> bytes:
    """Start calibration (weight mode; also timing and ratio modes on firmware V4.0.0+)."""
    return _command(0x09)


def set_auto_stop_condition(mode: AutoMode, condition: StopCondition) -> bytes:
    """Set what ends an automatic brew in timing or ratio mode. Firmware V4.0.0+ only."""
    return _command(
        0x0B,
        _choice("mode", mode, _AUTO_MODES),
        _choice("condition", condition, _STOP_CONDITIONS),
    )


def set_auto_stop_condition_legacy(condition: LegacyStopCondition) -> bytes:
    """Set what ends an automatic brew. Firmware V3.1.2 and earlier only."""
    return _command(0x0B, _choice("condition", condition, _LEGACY_STOP_CONDITIONS))


def set_powder_weight(grams: float) -> bytes:
    """Set the powder (dose) weight used by ratio mode: 0.1-999.9 g, rounded to 0.1 g.

    Firmware V3.2.4 beta or V4.0.0+.
    """
    tenths = round(grams * 10)
    if not 1 <= tenths <= 9999:
        raise ValueError(f"grams must be between 0.1 and 999.9, got {grams}")
    return _command(0x0D, tenths >> 8, tenths & 0xFF)


def shut_down() -> bytes:
    """Turn the scale off. Firmware V4.0.0+; not while charging."""
    return _command(0x15)


def reset_auto_off_countdown() -> bytes:
    """Restart the auto-off countdown without changing the auto-off time. Firmware V4.0.1+."""
    return _command(0x25)


# Data packets sent by the scale as notifications.


class ScaleData(BaseModel, frozen=True):
    """Live data the scale sends about ten times a second (packet type 0x0B)."""

    weight_g: Annotated[float, Field(description="Weight in grams, whatever the display unit.")]
    flow_rate_g_per_s: Annotated[float, Field(description="Flow rate in grams per second.")]
    timer_ms: Annotated[int, Field(description="Timer reading in milliseconds.")]
    battery_percent: Annotated[int, Field(description="Remaining battery charge.")]
    display_unit: Annotated[str, Field(description='Unit shown on the display: "g" or "oz".')]
    auto_off_time_min: Annotated[
        float, Field(description="Idle minutes before the scale turns itself off, as set with set_auto_off_time.")
    ]
    beep_level: Annotated[int, Field(description="Beep volume: 0 = mute, 1 = low, 2 = medium, 3 = high.")]
    flow_smoothing: Annotated[bool, Field(description="Whether flow-rate smoothing is on.")]


class AutoModeEvent(BaseModel, frozen=True):
    """Automatic-mode event and settlement data (packet type 0x0D)."""

    event: Annotated[
        str,
        Field(
            description='"ready" (entered ready), "started" (brewing started), "stopped" (brewing ended), '
            '"exit_ready" or "exit_done".'
        ),
    ]
    timer_ms: Annotated[int, Field(description="Timer reading in milliseconds.")]
    weight_g: Annotated[float, Field(description="Weight in grams.")]
    result: Annotated[
        float,
        Field(description="Average flow rate in g/s (timing mode) or liquid/powder weight ratio (ratio mode)."),
    ]


class PowderWeight(BaseModel, frozen=True):
    """Powder (dose) weight used by ratio mode (packet type 0x0F)."""

    weight_g: Annotated[float, Field(description="Powder weight in grams.")]


Packet = ScaleData | AutoModeEvent | PowderWeight


def parse_packet(data: bytes) -> Packet | None:
    """Decode a notification from the scale.

    Returns:
        The decoded packet, or ``None`` for packets the public protocol does not document.

    Raises:
        ValueError: If a documented packet has the wrong length or checksum.
    """
    if len(data) < 2 or data[0] != PRODUCT_NUMBER:
        return None
    kind = data[1]
    if kind not in (SCALE_DATA, AUTO_MODE_EVENT, POWDER_WEIGHT):
        return None
    if len(data) != DATA_PACKET_LENGTH or checksum(data[:-1]) != data[-1]:
        raise ValueError(f"Malformed packet: {data.hex(' ')}")

    if kind == SCALE_DATA:
        return ScaleData(
            weight_g=_signed(data[6], data[7:10]),
            flow_rate_g_per_s=_signed(data[10], data[11:13]),
            timer_ms=_uint(data[2:5]),
            battery_percent=data[13],
            display_unit=_UNITS.get(data[5], f"0x{data[5]:02x}"),
            auto_off_time_min=_uint(data[14:16]) / 10,  # "standby time" in the protocol document
            beep_level=data[16],
            flow_smoothing=bool(data[17]),
        )
    if kind == AUTO_MODE_EVENT:
        return AutoModeEvent(
            event=_EVENTS.get(data[2], f"0x{data[2]:02x}"),
            timer_ms=_uint(data[3:6]),
            weight_g=_signed(data[6], data[7:10]),
            result=_signed(data[10], data[11:13]),
        )
    return PowderWeight(weight_g=_signed(data[2], data[3:6]))


def _command(code: int, data2: int = 0, data3: int = 0) -> bytes:
    packet = bytes([PRODUCT_NUMBER, COMMAND, code, data2, data3])
    return packet + bytes([checksum(packet)])


def _in_range(name: str, value: int, low: int, high: int) -> int:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _choice(name: str, value: str, options: dict[str, int]) -> int:
    if value not in options:
        raise ValueError(f"{name} must be one of {', '.join(options)}, got {value!r}")
    return options[value]


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _signed(sign: int, magnitude: bytes) -> float:
    """Decode a ``+``/``-`` sign byte and a big-endian magnitude in hundredths."""
    value = _uint(magnitude) / 100
    return -value if sign == ord("-") and value else value
