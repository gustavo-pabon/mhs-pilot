import pytest

from technic_hub_mcp import protocol
from technic_hub_mcp.protocol import (
    AttachedIO,
    EndState,
    GenericError,
    HubPropertyUpdate,
    OutputFeedback,
    PortInputFormat,
    PortValue,
    ValueFormat,
)

# Messages a Technic Hub (firmware 1.0.00.0000) sent while connected.
REAL_ATTACHED_MOTOR = "0f 00 04 00 01 2e 00 00 10 00 00 00 10 00 00"
REAL_ATTACHED_LIGHT = "0f 00 04 32 01 17 00 00 00 00 10 00 00 00 10"
REAL_PROPERTIES = {
    "10 00 01 01 06 54 65 63 68 6e 69 63 20 48 75 62": (0x01, "Technic Hub"),
    "09 00 01 03 06 00 00 00 10": (0x03, "1.0.00.0000"),
    "09 00 01 04 06 00 00 00 08": (0x04, "0.8.00.0000"),
    "06 00 01 05 06 49": (0x05, 73),
    "06 00 01 06 06 64": (0x06, 100),
    "11 00 01 09 06 33 5f 30 32 5f 30 30 5f 76 31 2e 31": (0x09, "3_02_00_v1.1"),
    "07 00 01 0a 06 00 03": (0x0A, "3.00"),
    "06 00 01 0b 06 80": (0x0B, 128),
}


def parse(message: str) -> protocol.Message | None:
    return protocol.parse_message(bytes.fromhex(message))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Sent to the hub during hardware tests, which it answered or carried out
        (protocol.request_hub_property(protocol.HubProperty.ADVERTISING_NAME), "05 00 01 01 05"),
        (protocol.request_port_information(0, protocol.PortInformationType.PORT_VALUE), "05 00 21 00 00"),
        (protocol.set_port_input_format(0, 2, 1, notifications=True), "0a 00 41 00 02 01 00 00 00 01"),
        (protocol.start_speed_for_degrees(0, 45, 20, 50, EndState.BRAKE), "0e 00 81 00 11 0b 2d 00 00 00 14 32 7f 00"),
        (protocol.start_speed_for_degrees(0, 45, -20, 50, EndState.HOLD), "0e 00 81 00 11 0b 2d 00 00 00 ec 32 7e 00"),
        (protocol.start_power(0, EndState.BRAKE), "08 00 81 00 11 51 00 7f"),
        # Built from the protocol document
        (protocol.set_rgb_colors(0x32, 0x30, 0x47, 0x55), "0a 00 81 32 11 51 01 30 47 55"),
        (protocol.set_rgb_color_no(0x32, 9), "08 00 81 32 11 51 00 09"),
        (protocol.preset_encoder(1, -90), "0b 00 81 01 11 51 02 a6 ff ff ff"),
        (protocol.preset_encoder_2(16, 1, -1), "0e 00 81 10 11 14 01 00 00 00 ff ff ff ff"),
        (protocol.start_power(2, -100), "08 00 81 02 11 51 00 9c"),
        (protocol.start_power_2(16, 50, -50), "08 00 81 10 11 02 32 ce"),
        (protocol.set_acceleration_time(0, 1000), "09 00 81 00 11 05 e8 03 00"),
        (protocol.set_deceleration_time(0, 500, profile=1), "09 00 81 00 11 06 f4 01 01"),
        (protocol.start_speed(3, 0, 100, protocol.ACCELERATION_PROFILE), "09 00 81 03 11 07 00 64 01"),
        (protocol.start_speed_2(16, 30, -30, 80, 3), "0a 00 81 10 11 08 1e e2 50 03"),
        (protocol.start_speed_for_time(0, 2000, 50, 100, EndState.FLOAT), "0c 00 81 00 11 09 d0 07 32 64 00 00"),
        (
            protocol.start_speed_for_time_2(16, 300, 10, 20, 90, EndState.HOLD, 2),
            "0d 00 81 10 11 0a 2c 01 0a 14 5a 7e 02",
        ),
        (protocol.start_speed_for_degrees_2(16, 160, 55, -48), "0f 00 81 10 11 0c a0 00 00 00 37 d0 64 7f 00"),
        (protocol.goto_absolute_position(1, -720, 40), "0e 00 81 01 11 0d 30 fd ff ff 28 64 7f 00"),
        (protocol.goto_absolute_position_2(16, 90, 180, 40), "12 00 81 10 11 0e 5a 00 00 00 b4 00 00 00 28 64 7f 00"),
        (protocol.preset_tilt_impact_count(0x63, 1, 0), "0b 00 81 63 11 51 01 00 00 00 00"),
        (protocol.configure_tilt_impact(0x63, 2, 20, 1270), "09 00 81 63 11 51 02 14 7f"),
        (protocol.set_advertising_name("Cell"), "09 00 01 01 01 43 65 6c 6c"),
        (protocol.reset_advertising_name(), "05 00 01 01 04"),
        (protocol.hub_action(protocol.HubAction.SWITCH_OFF), "04 00 02 01"),
        (protocol.request_hub_alert(protocol.AlertType.LOW_VOLTAGE), "05 00 03 01 03"),
        (protocol.request_port_mode_information(0, 2, protocol.ModeInformationType.VALUE_FORMAT), "06 00 22 00 02 80"),
        (protocol.connect_virtual_port(0, 1), "06 00 61 01 00 01"),
        (protocol.disconnect_virtual_port(16), "05 00 61 00 10"),
    ],
)
def test_message_bytes(message: bytes, expected: str) -> None:
    assert message.hex(" ") == expected


def test_long_messages_take_two_length_bytes() -> None:
    message = protocol.message(protocol.MessageType.PORT_MODE_INFORMATION, bytes(200))

    assert message[:2] == bytes([0x80 | 204 & 0x7F, 204 >> 7])
    assert protocol.split_messages(message + message) == [message, message]
    assert parse(message.hex()).payload == bytes(197)


@pytest.mark.parametrize(
    "build",
    [
        lambda: protocol.start_power(0, 101),
        lambda: protocol.start_speed(0, -101),
        lambda: protocol.start_speed_for_time(0, 32768, 50),
        lambda: protocol.start_speed_for_degrees(0, 0, 50),
        lambda: protocol.goto_absolute_position(0, 0, 0),
        lambda: protocol.start_speed_for_degrees(0, 90, 50, end_state=1),
        lambda: protocol.set_acceleration_time(0, 10001),
        lambda: protocol.set_rgb_color_no(50, 11),
        lambda: protocol.configure_tilt_impact(99, 2, 128, 100),
        lambda: protocol.set_advertising_name("A name that is too long"),
        lambda: protocol.set_advertising_name("Lego™"),
    ],
)
def test_rejects_values_outside_the_protocol(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_parses_real_messages() -> None:
    assert parse(REAL_ATTACHED_MOTOR) == AttachedIO(0, 1, 0x2E, "0.0.00.1000", "0.0.00.1000")
    assert parse(REAL_ATTACHED_LIGHT).io_type == protocol.RGB_LIGHT
    assert parse("0a 00 47 00 02 01 00 00 00 01") == PortInputFormat(0, 2, 1, True)
    assert parse("08 00 45 00 cf ff ff ff") == PortValue(0, bytes.fromhex("cf ff ff ff"))
    assert parse("05 00 82 00 0a") == OutputFeedback(((0, 0x0A),))
    for message, (prop, value) in REAL_PROPERTIES.items():
        update = parse(message)
        assert isinstance(update, HubPropertyUpdate) and update.property == prop
        assert protocol.decode_hub_property(prop, update.payload) == value


def test_parses_other_messages() -> None:
    assert parse("05 00 04 00 00") == AttachedIO(0, 0)
    assert parse("09 00 04 10 02 2e 00 00 01") == AttachedIO(0x10, 2, 0x2E, port_a=0, port_b=1)
    assert parse("05 00 05 81 06") == GenericError(0x81, 0x06)
    assert parse("06 00 03 01 04 ff").active
    assert parse("0b 00 43 00 01 0f 06 1e 00 1f 00") == protocol.PortInformation(0, 0x0F, 6, 0x1E, 0x1F)
    assert parse("09 00 43 00 02 06 00 00 00").combinations == (6, 0)
    assert parse("07 00 82 00 0a 01 01") == OutputFeedback(((0, 0x0A), (1, 0x01)))
    assert parse("04 00 02 30") == protocol.HubActionUpdate(protocol.HubAction.WILL_SWITCH_OFF)
    assert parse("05 00 08 02 01") is None  # hardware network command: not covered


@pytest.mark.parametrize("message", ["", "05 00 82 00", "04 00 82 00", "06 00 82 00 01 02", "07 00 04 00 01 2e 00"])
def test_rejects_malformed_messages(message: str) -> None:
    with pytest.raises(ValueError):
        parse(message)


def test_decodes_versions_and_addresses() -> None:
    assert protocol.decode_version(bytes([0x10, 0x15, 0x37, 0x17])) == "1.7.37.1510"  # example in the protocol
    assert protocol.decode_mac_address(bytes.fromhex("90842b0a1b2c")) == "90:84:2B:0A:1B:2C"
    assert protocol.decode_text(b"POS\0\0\0") == "POS"
    assert protocol.parse_advertisement(bytes.fromhex("008006004100")) == protocol.Advertisement(
        False, 0x80, 0x06, 0x00, 0x41
    )
    assert protocol.decode_flags(0x06, protocol.HUB_CAPABILITIES) == ["peripheral_role", "lpf2_devices"]


def test_decodes_values() -> None:
    position = ValueFormat(datasets=1, dataset_type=2, figures=4, decimals=0)
    tilt = ValueFormat(datasets=3, dataset_type=1, figures=3, decimals=0)

    assert protocol.decode_values(bytes.fromhex("cf ff ff ff"), position) == [-49]
    assert protocol.decode_values(bytes.fromhex("01 00 ff ff 5a 00"), tilt) == [1, -1, 90]
    assert protocol.decode_value_format(bytes([3, 1, 3, 0])) == tilt
    assert protocol.decode_mode_combinations((0b10110, 0b11, 0, 0b1)) == [[1, 2, 4], [0, 1]]
    with pytest.raises(ValueError):
        protocol.decode_values(bytes(2), position)
