"""LEGO Wireless Protocol 3.0 (LWP3): message builders and parsers for the LEGO Technic Hub.

Pure functions with no Bluetooth dependency, implementing LEGO's published protocol:
https://lego.github.io/lego-ble-wireless-protocol-docs/

The hub has a single GATT characteristic: messages to the hub are written to it, and the hub's messages arrive as
notifications on it. Every message starts with a common header (the message length, a hub ID that is always 0 and
the message type). Multi-byte numbers are little-endian unless noted.

Not covered, on purpose: firmware update and memory lock messages (0x10-0x13), hardware network commands (0x08),
the production-only fast shutdown action (0x2F), combined-mode sensor streams (0x42, 0x46, 0x48) and raw WriteDirect
(sub-command 0x50, which carries the hardware reset and the tilt sensor's factory calibration). Port values are read
on request rather than streamed.
"""

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Literal, get_args

SERVICE_UUID = "00001623-1212-efde-1623-785feabcd123"
CHARACTERISTIC_UUID = "00001624-1212-efde-1623-785feabcd123"
LEGO_COMPANY_ID = 0x0397
"""Bluetooth company identifier of LEGO System A/S, the key of the hub's advertised manufacturer data."""
TECHNIC_HUB = 0x80
"""System type and device number of the Technic Hub (88012), in its advertisement and System Type ID property."""

HUB_ID = 0x00
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


class MessageType(IntEnum):
    HUB_PROPERTIES = 0x01
    HUB_ACTIONS = 0x02
    HUB_ALERTS = 0x03
    HUB_ATTACHED_IO = 0x04
    GENERIC_ERROR = 0x05
    PORT_INFORMATION_REQUEST = 0x21
    PORT_MODE_INFORMATION_REQUEST = 0x22
    PORT_INPUT_FORMAT_SETUP = 0x41
    PORT_INFORMATION = 0x43
    PORT_MODE_INFORMATION = 0x44
    PORT_VALUE = 0x45
    PORT_INPUT_FORMAT = 0x47
    VIRTUAL_PORT_SETUP = 0x61
    PORT_OUTPUT_COMMAND = 0x81
    PORT_OUTPUT_COMMAND_FEEDBACK = 0x82


class HubProperty(IntEnum):
    ADVERTISING_NAME = 0x01
    BUTTON = 0x02
    FW_VERSION = 0x03
    HW_VERSION = 0x04
    RSSI = 0x05
    BATTERY_VOLTAGE = 0x06
    BATTERY_TYPE = 0x07
    MANUFACTURER_NAME = 0x08
    RADIO_FIRMWARE_VERSION = 0x09
    LWP_PROTOCOL_VERSION = 0x0A
    SYSTEM_TYPE_ID = 0x0B
    HW_NETWORK_ID = 0x0C
    PRIMARY_MAC_ADDRESS = 0x0D
    SECONDARY_MAC_ADDRESS = 0x0E
    HW_NETWORK_FAMILY = 0x0F


class PropertyOperation(IntEnum):
    SET = 0x01
    ENABLE_UPDATES = 0x02
    DISABLE_UPDATES = 0x03
    RESET = 0x04
    REQUEST_UPDATE = 0x05
    UPDATE = 0x06


class HubAction(IntEnum):
    SWITCH_OFF = 0x01
    DISCONNECT = 0x02
    VCC_PORT_CONTROL_ON = 0x03
    VCC_PORT_CONTROL_OFF = 0x04
    ACTIVATE_BUSY_INDICATION = 0x05
    RESET_BUSY_INDICATION = 0x06
    # Sent by the hub
    WILL_SWITCH_OFF = 0x30
    WILL_DISCONNECT = 0x31
    WILL_GO_INTO_BOOT_MODE = 0x32


class AlertType(IntEnum):
    LOW_VOLTAGE = 0x01
    HIGH_CURRENT = 0x02
    LOW_SIGNAL_STRENGTH = 0x03
    OVER_POWER_CONDITION = 0x04


ALERT_REQUEST_UPDATE = 0x03
ALERT_UPDATE = 0x04


class IOEvent(IntEnum):
    DETACHED = 0x00
    ATTACHED = 0x01
    ATTACHED_VIRTUAL = 0x02


class PortInformationType(IntEnum):
    PORT_VALUE = 0x00
    MODE_INFO = 0x01
    POSSIBLE_MODE_COMBINATIONS = 0x02


class ModeInformationType(IntEnum):
    NAME = 0x00
    RAW = 0x01
    PCT = 0x02
    SI = 0x03
    SYMBOL = 0x04
    MAPPING = 0x05
    MOTOR_BIAS = 0x07
    CAPABILITY_BITS = 0x08
    VALUE_FORMAT = 0x80


class Feedback(IntFlag):
    """Port Output Command Feedback bits: the state of a port's command buffer."""

    IN_PROGRESS = 0x01  # buffer empty, command in progress
    COMPLETED = 0x02  # buffer empty, command completed
    DISCARDED = 0x04  # current command(s) discarded
    IDLE = 0x08
    BUSY_FULL = 0x10


class EndState(IntEnum):
    """How a motor stops at the end of a command."""

    FLOAT = 0
    HOLD = 126
    BRAKE = 127


ACCELERATION_PROFILE = 0x01
DECELERATION_PROFILE = 0x02
EXECUTE_IMMEDIATELY = 0x10
COMMAND_FEEDBACK = 0x01
WRITE_DIRECT_MODE_DATA = 0x51

ERROR_CODES = {
    0x01: "ACK",
    0x02: "MACK",
    0x03: "buffer overflow",
    0x04: "timeout",
    0x05: "command not recognized",
    0x06: "invalid use (wrong parameters)",
    0x07: "overcurrent",
    0x08: "internal error",
}

SYSTEM_TYPES = {
    0x00: "WeDo 2.0 Hub",
    0x20: "Duplo Train Base",
    0x40: "Boost Move Hub",
    0x41: "2-port Hub",
    0x42: "2-port Handset",
    TECHNIC_HUB: "Technic Hub",
}

IO_TYPES = {
    # From the LWP3 documentation
    0x0001: "Motor",
    0x0002: "System Train Motor",
    0x0005: "Button",
    0x0008: "LED Light",
    0x0014: "Voltage Sensor",
    0x0015: "Current Sensor",
    0x0016: "Piezo Tone",
    0x0017: "RGB Light",
    0x0022: "Tilt Sensor",
    0x0023: "Motion Sensor",
    0x0025: "Color & Distance Sensor",
    0x0026: "Medium Linear Motor",
    0x0027: "Move Hub Internal Motor",
    0x0028: "Move Hub Internal Tilt Sensor",
    # Newer devices, as identified by the node-poweredup library
    0x0029: "Duplo Train Base Motor",
    0x002A: "Duplo Train Base Speaker",
    0x002B: "Duplo Train Base Color Sensor",
    0x002C: "Duplo Train Base Speedometer",
    0x002E: "Technic Large Motor",
    0x002F: "Technic XL Motor",
    0x0030: "Technic Medium Angular Motor",
    0x0031: "Technic Large Angular Motor",
    0x0036: "Technic Hub Gesture Sensor",
    0x0037: "Remote Control Button",
    0x0038: "Remote Control Signal Strength",
    0x0039: "Technic Hub Accelerometer",
    0x003A: "Technic Hub Gyro Sensor",
    0x003B: "Technic Hub Tilt Sensor",
    0x003C: "Technic Hub Temperature Sensor",
    0x003D: "Technic Color Sensor",
    0x003E: "Technic Distance Sensor",
    0x003F: "Technic Force Sensor",
    0x0040: "Technic 3x3 Color Light Matrix",
    0x0041: "Technic Small Angular Motor",
    0x004B: "Technic Medium Angular Motor (gray)",
    0x004C: "Technic Large Angular Motor (gray)",
}

MOTORS = frozenset({0x0001, 0x0002, 0x0026, 0x0027, 0x0029, 0x002E, 0x002F, 0x0030, 0x0031, 0x0041, 0x004B, 0x004C})
RGB_LIGHT = 0x0017
TECHNIC_HUB_TILT_SENSOR = 0x003B

LedColor = Literal["off", "pink", "purple", "blue", "light_blue", "cyan", "green", "yellow", "orange", "red", "white"]
LED_COLORS: tuple[str, ...] = get_args(LedColor)
"""Color numbers 0-10 of an RGB light (SetRgbColorNo), in order."""

HUB_CAPABILITIES = {0x01: "central_role", 0x02: "peripheral_role", 0x04: "lpf2_devices", 0x08: "remote_controller"}
HUB_STATUS = {0x01: "can_be_peripheral", 0x02: "can_be_central", 0x20: "request_window", 0x40: "request_connect"}
PORT_CAPABILITIES = {0x01: "output", 0x02: "input", 0x04: "combinable", 0x08: "synchronizable"}
MAPPING_FLAGS = {0x80: "null_value", 0x40: "functional_mapping_2", 0x10: "absolute", 0x08: "relative", 0x04: "discrete"}
DATASET_TYPES = {0: ("int8", "b"), 1: ("int16", "h"), 2: ("int32", "i"), 3: ("float", "f")}
BATTERY_TYPES = {0x00: "normal", 0x01: "rechargeable"}


# Messages sent by the hub, decoded by parse_message().


@dataclass(frozen=True)
class HubPropertyUpdate:
    property: int
    payload: bytes


@dataclass(frozen=True)
class HubActionUpdate:
    action: int


@dataclass(frozen=True)
class HubAlertUpdate:
    alert: int
    active: bool


@dataclass(frozen=True)
class AttachedIO:
    """A device attached to or detached from a port, or a virtual port formed by two ports."""

    port: int
    event: int
    io_type: int | None = None
    hardware_revision: str | None = None
    software_revision: str | None = None
    port_a: int | None = None
    port_b: int | None = None


@dataclass(frozen=True)
class GenericError:
    command: int
    code: int


@dataclass(frozen=True)
class PortInformation:
    """Port Information (0x43), Mode Info: what a port's device can do."""

    port: int
    capabilities: int
    mode_count: int
    input_modes: int
    output_modes: int


@dataclass(frozen=True)
class PortModeCombinations:
    """Port Information (0x43), Possible Mode Combinations: one bit mask of modes per combination."""

    port: int
    combinations: tuple[int, ...]


@dataclass(frozen=True)
class PortModeInformation:
    port: int
    mode: int
    info_type: int
    payload: bytes


@dataclass(frozen=True)
class PortValue:
    port: int
    data: bytes


@dataclass(frozen=True)
class PortInputFormat:
    port: int
    mode: int
    delta_interval: int
    notifications: bool


@dataclass(frozen=True)
class OutputFeedback:
    feedback: tuple[tuple[int, int], ...]
    """(port, Feedback bits) pairs."""


@dataclass(frozen=True)
class ValueFormat:
    datasets: int
    dataset_type: int
    figures: int
    decimals: int


@dataclass(frozen=True)
class Advertisement:
    """LEGO manufacturer data the hub advertises while it waits for a connection."""

    button_pressed: bool
    system_type: int
    capabilities: int
    last_network: int
    status: int


Message = (
    HubPropertyUpdate
    | HubActionUpdate
    | HubAlertUpdate
    | AttachedIO
    | GenericError
    | PortInformation
    | PortModeCombinations
    | PortModeInformation
    | PortValue
    | PortInputFormat
    | OutputFeedback
)


def message(kind: int, payload: bytes = b"") -> bytes:
    """Frame ``payload`` as a message of type ``kind``, adding the common header."""
    body = bytes([HUB_ID, kind]) + payload
    if len(body) + 1 <= 0x7F:
        return bytes([len(body) + 1]) + body
    length = len(body) + 2  # lengths over 127 take two bytes: 7 low bits (bit 7 set), then the high bits
    return bytes([0x80 | (length & 0x7F), length >> 7]) + body


# Hub properties, actions and alerts


def request_hub_property(prop: int) -> bytes:
    """Ask the hub for the current value of a property; it answers with a Hub Property update."""
    return message(MessageType.HUB_PROPERTIES, bytes([prop, PropertyOperation.REQUEST_UPDATE]))


def set_advertising_name(name: str) -> bytes:
    """Rename the hub: 1-14 printable ASCII characters, kept by the hub and used in its advertisement."""
    if not 1 <= len(name) <= 14 or not all(" " <= c <= "~" for c in name):
        raise ValueError(f"name must be 1-14 printable ASCII characters, got {name!r}")
    payload = bytes([HubProperty.ADVERTISING_NAME, PropertyOperation.SET]) + name.encode("ascii")
    return message(MessageType.HUB_PROPERTIES, payload)


def reset_advertising_name() -> bytes:
    """Restore the hub's default name."""
    return message(MessageType.HUB_PROPERTIES, bytes([HubProperty.ADVERTISING_NAME, PropertyOperation.RESET]))


def hub_action(action: int) -> bytes:
    """Ask the hub to perform an action, such as switching off."""
    return message(MessageType.HUB_ACTIONS, bytes([action]))


def request_hub_alert(alert: int) -> bytes:
    """Ask for the current state of an alert; the hub answers with a Hub Alert update."""
    return message(MessageType.HUB_ALERTS, bytes([alert, ALERT_REQUEST_UPDATE]))


# Ports


def request_port_information(port: int, info_type: int) -> bytes:
    """Ask for a port's value (in its current mode), its mode info or its possible mode combinations."""
    return message(MessageType.PORT_INFORMATION_REQUEST, bytes([_port(port), info_type]))


def request_port_mode_information(port: int, mode: int, info_type: int) -> bytes:
    """Ask for one piece of information about one mode of a port's device: name, ranges, unit, value format..."""
    return message(MessageType.PORT_MODE_INFORMATION_REQUEST, bytes([_port(port), _byte("mode", mode), info_type]))


def set_port_input_format(port: int, mode: int, delta_interval: int = 1, notifications: bool = False) -> bytes:
    """Put a port's device in ``mode``; with notifications on, the hub sends a value whenever it changes by delta."""
    payload = struct.pack(
        "<BBIB",
        _port(port),
        _byte("mode", mode),
        _in_range("delta_interval", delta_interval, 0, 2**32 - 1),
        notifications,
    )
    return message(MessageType.PORT_INPUT_FORMAT_SETUP, payload)


def connect_virtual_port(port_a: int, port_b: int) -> bytes:
    """Combine two synchronizable ports, such as two motors, into a virtual port with its own port ID."""
    return message(MessageType.VIRTUAL_PORT_SETUP, bytes([0x01, _port(port_a), _port(port_b)]))


def disconnect_virtual_port(port: int) -> bytes:
    """Remove a virtual port."""
    return message(MessageType.VIRTUAL_PORT_SETUP, bytes([0x00, _port(port)]))


# Port output commands (0x81). Each runs immediately, replacing the port's current command, and asks the hub for
# feedback. Speeds and powers are percentages; a positive value turns clockwise. Commands with two values (power,
# speed or position) are for a virtual port: the first value is for its first motor, the second for the other.


def start_power(port: int, power: int) -> bytes:
    """Run a motor at an unregulated power, -100 to 100; 0 lets it float, EndState.BRAKE (127) brakes it."""
    if power != EndState.BRAKE:
        _in_range("power", power, -100, 100)
    return write_direct_mode_data(port, 0x00, struct.pack("<b", power))


def start_power_2(port: int, power_1: int, power_2: int) -> bytes:
    """Run the two motors of a virtual port at their own unregulated powers (StartPower(Power1, Power2))."""
    return _output(port, 0x02, struct.pack("<bb", _power(power_1), _power(power_2)))


def set_acceleration_time(port: int, time_ms: int, profile: int = 0) -> bytes:
    """Set the time to accelerate from 0 to 100 % in an acceleration profile, used by commands that ask for it."""
    return _output(port, 0x05, struct.pack("<hb", _in_range("time_ms", time_ms, 0, 10000), _profile_number(profile)))


def set_deceleration_time(port: int, time_ms: int, profile: int = 0) -> bytes:
    """Set the time to decelerate from 100 % to 0 in a deceleration profile, used by commands that ask for it."""
    return _output(port, 0x06, struct.pack("<hb", _in_range("time_ms", time_ms, 0, 10000), _profile_number(profile)))


def start_speed(port: int, speed: int, max_power: int = 100, use_profile: int = 0) -> bytes:
    """Run a motor at a regulated speed until another command; speed 0 holds its position."""
    return _output(port, 0x07, struct.pack("<bbB", _speed(speed), _max_power(max_power), _use_profile(use_profile)))


def start_speed_2(port: int, speed_1: int, speed_2: int, max_power: int = 100, use_profile: int = 0) -> bytes:
    """Run the two motors of a virtual port at their own regulated speeds."""
    payload = struct.pack("<bbbB", _speed(speed_1), _speed(speed_2), _max_power(max_power), _use_profile(use_profile))
    return _output(port, 0x08, payload)


def start_speed_for_time(
    port: int, time_ms: int, speed: int, max_power: int = 100, end_state: int = EndState.BRAKE, use_profile: int = 0
) -> bytes:
    """Run a motor at a regulated speed for ``time_ms`` milliseconds (0-32767), then stop it with ``end_state``."""
    payload = struct.pack(
        "<hbbBB", _time(time_ms), _speed(speed), _max_power(max_power), _end_state(end_state), _use_profile(use_profile)
    )
    return _output(port, 0x09, payload)


def start_speed_for_time_2(
    port: int,
    time_ms: int,
    speed_1: int,
    speed_2: int,
    max_power: int = 100,
    end_state: int = EndState.BRAKE,
    use_profile: int = 0,
) -> bytes:
    """Run the two motors of a virtual port at their own speeds for ``time_ms`` milliseconds."""
    payload = struct.pack(
        "<hbbbBB",
        _time(time_ms),
        _speed(speed_1),
        _speed(speed_2),
        _max_power(max_power),
        _end_state(end_state),
        _use_profile(use_profile),
    )
    return _output(port, 0x0A, payload)


def start_speed_for_degrees(
    port: int, degrees: int, speed: int, max_power: int = 100, end_state: int = EndState.BRAKE, use_profile: int = 0
) -> bytes:
    """Turn a motor by ``degrees`` (1 or more) at ``speed``, whose sign sets the direction, then stop it."""
    payload = struct.pack(
        "<ibbBB",
        _in_range("degrees", degrees, 1, INT32_MAX),
        _speed(speed),
        _max_power(max_power),
        _end_state(end_state),
        _use_profile(use_profile),
    )
    return _output(port, 0x0B, payload)


def start_speed_for_degrees_2(
    port: int,
    degrees: int,
    speed_1: int,
    speed_2: int,
    max_power: int = 100,
    end_state: int = EndState.BRAKE,
    use_profile: int = 0,
) -> bytes:
    """Turn the two motors of a virtual port at their own speeds; ``degrees`` is their average travel."""
    payload = struct.pack(
        "<ibbbBB",
        _in_range("degrees", degrees, 0, 10_000_000),
        _speed(speed_1),
        _speed(speed_2),
        _max_power(max_power),
        _end_state(end_state),
        _use_profile(use_profile),
    )
    return _output(port, 0x0C, payload)


def goto_absolute_position(
    port: int, position: int, speed: int, max_power: int = 100, end_state: int = EndState.BRAKE, use_profile: int = 0
) -> bytes:
    """Turn a motor to ``position`` (encoder degrees) at ``speed``, 1-100, then stop it with ``end_state``."""
    payload = struct.pack(
        "<ibbBB",
        _position(position),
        _in_range("speed", speed, 1, 100),
        _max_power(max_power),
        _end_state(end_state),
        _use_profile(use_profile),
    )
    return _output(port, 0x0D, payload)


def goto_absolute_position_2(
    port: int,
    position_1: int,
    position_2: int,
    speed: int,
    max_power: int = 100,
    end_state: int = EndState.BRAKE,
    use_profile: int = 0,
) -> bytes:
    """Turn the two motors of a virtual port to their own positions."""
    payload = struct.pack(
        "<iibbBB",
        _position(position_1),
        _position(position_2),
        _in_range("speed", speed, 1, 100),
        _max_power(max_power),
        _end_state(end_state),
        _use_profile(use_profile),
    )
    return _output(port, 0x0E, payload)


def preset_encoder(port: int, position: int) -> bytes:
    """Set a motor's encoder to ``position`` degrees (0 resets it); on a virtual port, all its encoders."""
    return write_direct_mode_data(port, 0x02, struct.pack("<i", _position(position)))


def preset_encoder_2(port: int, position_1: int, position_2: int) -> bytes:
    """Set the encoders of a virtual port's two motors, leaving the virtual port's own encoder as it is."""
    return _output(port, 0x14, struct.pack("<ii", _position(position_1), _position(position_2)))


def set_rgb_color_no(port: int, color: int) -> bytes:
    """Show color number ``color`` (0-10, see LED_COLORS) on an RGB light; it needs the light in mode 0."""
    return write_direct_mode_data(port, 0x00, bytes([_in_range("color", color, 0, len(LED_COLORS) - 1)]))


def set_rgb_colors(port: int, red: int, green: int, blue: int) -> bytes:
    """Mix a color on an RGB light from red, green and blue, 0-255 each; it needs the light in mode 1."""
    return write_direct_mode_data(port, 0x01, bytes([_byte("red", red), _byte("green", green), _byte("blue", blue)]))


def preset_tilt_impact_count(port: int, mode: int, count: int) -> bytes:
    """Set a tilt sensor's impact counter (TiltImpactPreset), written to its impact-count mode."""
    return write_direct_mode_data(port, mode, struct.pack("<i", _in_range("count", count, 0, INT32_MAX)))


def configure_tilt_impact(port: int, mode: int, threshold: int, holdoff_ms: int) -> bytes:
    """Set the size of an impact (bump) and the minimum time between two (TiltConfigImpact), 10-1270 ms."""
    holdoff = round(_in_range("holdoff_ms", holdoff_ms, 10, 1270) / 10)  # sent in steps of 10 ms
    return write_direct_mode_data(port, mode, bytes([_in_range("threshold", threshold, 0, 127), holdoff]))


def write_direct_mode_data(port: int, mode: int, payload: bytes) -> bytes:
    """WriteDirectModeData: send ``payload`` to a port's device in ``mode``."""
    return _output(port, WRITE_DIRECT_MODE_DATA, bytes([_byte("mode", mode)]) + payload)


# Parsing


def split_messages(data: bytes) -> list[bytes]:
    """Split a notification into messages: a notification can carry more than one.

    Raises:
        ValueError: If the lengths don't add up.
    """
    messages = []
    while data:
        length, size = _read_length(data)
        if not size + 2 <= length <= len(data):
            raise ValueError(f"Malformed notification: {data.hex(' ')}")
        messages.append(data[:length])
        data = data[length:]
    return messages


def parse_message(data: bytes) -> Message | None:
    """Decode one message from the hub.

    Returns:
        The decoded message, or ``None`` for messages this module does not cover.

    Raises:
        ValueError: If the message is malformed.
    """
    length, size = _read_length(data)
    if length != len(data) or length < size + 2:
        raise ValueError(f"Malformed message: {data.hex(' ')}")
    try:
        return _parse_body(data[size + 1], data[size + 2 :])
    except (IndexError, struct.error, ValueError) as exc:
        raise ValueError(f"Malformed message: {data.hex(' ')}") from exc


def _parse_body(kind: int, body: bytes) -> Message | None:
    if kind == MessageType.HUB_PROPERTIES:
        return HubPropertyUpdate(body[0], bytes(body[2:])) if body[1] == PropertyOperation.UPDATE else None
    if kind == MessageType.HUB_ACTIONS:
        return HubActionUpdate(body[0])
    if kind == MessageType.HUB_ALERTS:
        return HubAlertUpdate(body[0], body[2] != 0x00) if body[1] == ALERT_UPDATE else None
    if kind == MessageType.HUB_ATTACHED_IO:
        port, event = body[0], body[1]
        if event == IOEvent.DETACHED:
            return AttachedIO(port, event)
        (io_type,) = struct.unpack_from("<H", body, 2)
        if event == IOEvent.ATTACHED:
            return AttachedIO(port, event, io_type, decode_version(body[4:8]), decode_version(body[8:12]))
        return AttachedIO(port, event, io_type, port_a=body[4], port_b=body[5])
    if kind == MessageType.GENERIC_ERROR:
        return GenericError(body[0], body[1])
    if kind == MessageType.PORT_INFORMATION:
        if body[1] == PortInformationType.MODE_INFO:
            return PortInformation(body[0], *struct.unpack_from("<BBHH", body, 2))
        if body[1] == PortInformationType.POSSIBLE_MODE_COMBINATIONS:
            count = (len(body) - 2) // 2
            return PortModeCombinations(body[0], struct.unpack_from(f"<{count}H", body, 2))
        return None
    if kind == MessageType.PORT_MODE_INFORMATION:
        return PortModeInformation(body[0], body[1], body[2], bytes(body[3:]))
    if kind == MessageType.PORT_VALUE:
        return PortValue(body[0], bytes(body[1:]))
    if kind == MessageType.PORT_INPUT_FORMAT:
        port, mode, delta, notifications = struct.unpack_from("<BBIB", body)
        return PortInputFormat(port, mode, delta, bool(notifications))
    if kind == MessageType.PORT_OUTPUT_COMMAND_FEEDBACK:
        if not body or len(body) % 2:
            raise ValueError("odd feedback length")
        return OutputFeedback(tuple(zip(body[0::2], body[1::2])))
    return None


def parse_advertisement(manufacturer_data: bytes) -> Advertisement:
    """Decode the LEGO manufacturer data of an advertisement (the bytes after the company identifier)."""
    if len(manufacturer_data) < 5:
        raise ValueError(f"Malformed manufacturer data: {manufacturer_data.hex(' ')}")
    button, system_type, capabilities, last_network, status = manufacturer_data[:5]
    return Advertisement(bool(button), system_type, capabilities, last_network, status)


# Decoding values


def decode_hub_property(prop: int, payload: bytes) -> str | int | bool:
    """Decode a Hub Property payload into a string, number or boolean."""
    if prop in (HubProperty.ADVERTISING_NAME, HubProperty.MANUFACTURER_NAME, HubProperty.RADIO_FIRMWARE_VERSION):
        return decode_text(payload)
    if prop in (HubProperty.FW_VERSION, HubProperty.HW_VERSION):
        return decode_version(payload)
    if prop == HubProperty.LWP_PROTOCOL_VERSION:
        return decode_lwp_version(payload)
    if prop in (HubProperty.PRIMARY_MAC_ADDRESS, HubProperty.SECONDARY_MAC_ADDRESS):
        return decode_mac_address(payload)
    if not payload:
        raise ValueError(f"Empty payload for hub property 0x{prop:02x}")
    if prop == HubProperty.BUTTON:
        return payload[0] != 0
    if prop == HubProperty.RSSI:
        return struct.unpack_from("<b", payload)[0]
    if prop == HubProperty.BATTERY_TYPE:
        return BATTERY_TYPES.get(payload[0], f"0x{payload[0]:02x}")
    return payload[0]


def decode_version(raw: bytes) -> str:
    """Decode a 4-byte version number: 0MMMmmmm, bug fix (BCD) and build number (BCD), as ``1.7.37.1510``."""
    if len(raw) != 4:
        raise ValueError(f"A version number takes 4 bytes, got {raw.hex(' ')}")
    v = int.from_bytes(raw, "little")
    return f"{v >> 28 & 0x7}.{v >> 24 & 0xF}.{v >> 16 & 0xFF:02x}.{v & 0xFFFF:04x}"


def decode_lwp_version(raw: bytes) -> str:
    """Decode the 2-byte BCD LEGO Wireless Protocol version, as ``3.00``."""
    if len(raw) != 2:
        raise ValueError(f"The protocol version takes 2 bytes, got {raw.hex(' ')}")
    return f"{raw[1]:x}.{raw[0]:02x}"


def decode_mac_address(raw: bytes) -> str:
    """Decode a 6-byte, big-endian MAC address."""
    if len(raw) != 6:
        raise ValueError(f"A MAC address takes 6 bytes, got {raw.hex(' ')}")
    return ":".join(f"{b:02X}" for b in raw)


def decode_text(raw: bytes) -> str:
    """Decode an ASCII string, which may be padded with zero bytes."""
    return raw.split(b"\0", 1)[0].decode("ascii", errors="replace").strip()


def decode_range(raw: bytes) -> tuple[float, float]:
    """Decode a RAW, PCT or SI range: minimum and maximum, as two 32-bit floats."""
    if len(raw) < 8:
        raise ValueError(f"A range takes 8 bytes, got {raw.hex(' ')}")
    low, high = struct.unpack_from("<ff", raw)
    return low, high


def decode_value_format(raw: bytes) -> ValueFormat:
    """Decode a mode's value format: number of values, their type, digits and decimals."""
    if len(raw) < 4 or raw[1] not in DATASET_TYPES:
        raise ValueError(f"Malformed value format: {raw.hex(' ')}")
    return ValueFormat(*raw[:4])


def decode_values(data: bytes, value_format: ValueFormat) -> list[int | float]:
    """Decode a Port Value payload with the value format of the port's current mode."""
    code = DATASET_TYPES[value_format.dataset_type][1]
    expected = value_format.datasets * struct.calcsize(code)
    if len(data) != expected:
        raise ValueError(f"Expected {expected} value bytes for this mode, got {len(data)}: {data.hex(' ')}")
    return list(struct.unpack(f"<{value_format.datasets}{code}", data))


def decode_flags(value: int, names: dict[int, str]) -> list[str]:
    """Return the names of the bits set in ``value``."""
    return [name for bit, name in names.items() if value & bit]


def decode_mode_combinations(combinations: tuple[int, ...]) -> list[list[int]]:
    """Turn mode-combination bit masks into lists of mode numbers, stopping at the first empty combination."""
    result = []
    for mask in combinations:
        if not mask:
            break
        result.append([mode for mode in range(16) if mask >> mode & 1])
    return result


def _read_length(data: bytes) -> tuple[int, int]:
    """Return a message's length and the size of its length field, one or two bytes."""
    if not data:
        raise ValueError("Empty message")
    if data[0] & 0x80:
        if len(data) < 2:
            raise ValueError(f"Truncated message: {data.hex(' ')}")
        return (data[0] & 0x7F) | data[1] << 7, 2
    return data[0], 1


def _output(port: int, sub_command: int, payload: bytes) -> bytes:
    startup = EXECUTE_IMMEDIATELY | COMMAND_FEEDBACK
    return message(MessageType.PORT_OUTPUT_COMMAND, bytes([_port(port), startup, sub_command]) + payload)


def _in_range(name: str, value: int, low: int, high: int) -> int:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _port(port: int) -> int:
    return _in_range("port", port, 0, 255)


def _byte(name: str, value: int) -> int:
    return _in_range(name, value, 0, 255)


def _power(power: int) -> int:
    return _in_range("power", power, -100, 100)


def _speed(speed: int) -> int:
    return _in_range("speed", speed, -100, 100)


def _max_power(max_power: int) -> int:
    return _in_range("max_power", max_power, 0, 100)


def _time(time_ms: int) -> int:
    return _in_range("time_ms", time_ms, 0, 32767)


def _position(position: int) -> int:
    return _in_range("position", position, INT32_MIN, INT32_MAX)


def _profile_number(profile: int) -> int:
    return _in_range("profile", profile, 0, 127)


def _use_profile(use_profile: int) -> int:
    return _in_range("use_profile", use_profile, 0, ACCELERATION_PROFILE | DECELERATION_PROFILE)


def _end_state(end_state: int) -> int:
    if end_state not in tuple(EndState):
        raise ValueError(f"end_state must be FLOAT (0), HOLD (126) or BRAKE (127), got {end_state}")
    return end_state
