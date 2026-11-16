"""Results of the MCP tools: immutable pydantic models whose field descriptions document the tools' output."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

PortName = Annotated[str, Field(description='How tools refer to the port: A-D, a pair such as "AB", or its number.')]


class HubAdvertisement(BaseModel, frozen=True):
    """What the hub advertised when this computer found it."""

    signal_dbm: Annotated[int | None, Field(description="Signal strength this computer measured, in dBm.")]
    button_pressed: Annotated[bool, Field(description="Whether the hub's button was pressed.")]
    system_type_id: Annotated[int, Field(description="System type and device number; 128 is the Technic Hub.")]
    capabilities: Annotated[
        list[str], Field(description="central_role, peripheral_role, lpf2_devices (has ports) or remote_controller.")
    ]
    last_network_id: Annotated[int, Field(description="ID of the last hardware network the hub joined; 0 if none.")]
    status: Annotated[
        list[str], Field(description="can_be_peripheral, can_be_central, request_window or request_connect.")
    ]


class HubInfo(BaseModel, frozen=True):
    """The hub's properties. A field is null when the hub did not report it."""

    name: Annotated[str | None, Field(description="Advertised name, at most 14 characters.")]
    address: Annotated[str, Field(description="Bluetooth address of the hub on this computer (a UUID on macOS).")]
    button_pressed: Annotated[bool | None, Field(description="Whether the hub's button is pressed now.")]
    firmware_version: str | None
    hardware_version: str | None
    rssi: Annotated[
        int | None, Field(description="Signal strength the hub reports for this computer; not calibrated dBm.")
    ]
    battery_percent: Annotated[int | None, Field(description="Remaining battery charge.")]
    battery_type: Annotated[str | None, Field(description='"normal" or "rechargeable".')]
    manufacturer: str | None
    radio_firmware_version: str | None
    lwp_version: Annotated[str | None, Field(description="Version of the LEGO Wireless Protocol the hub speaks.")]
    system_type_id: Annotated[int | None, Field(description="System type and device number; 128 is the Technic Hub.")]
    system_type: Annotated[str | None, Field(description="Hub model.")]
    hw_network_id: Annotated[int | None, Field(description="Hardware network ID, for hub-to-hub networks.")]
    hw_network_family: Annotated[int | None, Field(description="Hardware network family, 0-8.")]
    primary_mac_address: str | None
    secondary_mac_address: Annotated[str | None, Field(description="MAC address the hub's boot loader uses.")]
    advertisement: HubAdvertisement | None


class HubAlerts(BaseModel, frozen=True):
    """The hub's alerts: true when the alert is on, null when the hub did not report it."""

    low_voltage: bool | None
    high_current: bool | None
    low_signal_strength: bool | None
    over_power_condition: bool | None


class Port(BaseModel, frozen=True):
    """A port with a device attached."""

    port: PortName
    port_id: Annotated[int, Field(description="Port ID the hub uses.")]
    kind: Annotated[
        Literal["connector", "internal", "virtual"],
        Field(description="connector (A-D), internal (built into the hub) or virtual (two synchronized ports)."),
    ]
    device_type_id: Annotated[int, Field(description="IO type ID of the attached device.")]
    device: Annotated[str | None, Field(description="Name of the device, if known.")]
    hardware_revision: str | None = None
    software_revision: str | None = None
    pair: Annotated[list[str] | None, Field(description="The two ports of a virtual port.")] = None


class PortList(BaseModel, frozen=True):
    ports: list[Port]


class ValueRange(BaseModel, frozen=True):
    min: float
    max: float


class ValueFormat(BaseModel, frozen=True):
    datasets: Annotated[int, Field(description="How many values the mode reports at a time.")]
    dataset_type: Literal["int8", "int16", "int32", "float"]
    figures: Annotated[int, Field(description="Number of digits to show.")]
    decimals: Annotated[int, Field(description="Number of decimals to show.")]


class ModeInfo(BaseModel, frozen=True):
    """One mode of a port's device. A field is null when the device did not report it."""

    mode: int
    name: str | None
    input: Annotated[bool, Field(description="The mode reports values: read them with read_port_value.")]
    output: Annotated[bool, Field(description="The mode accepts values, such as motor power or a light color.")]
    raw_range: Annotated[ValueRange | None, Field(description="Range of the values the device sends.")]
    percent_range: Annotated[ValueRange | None, Field(description="The raw range, as a percentage.")]
    si_range: Annotated[ValueRange | None, Field(description="The raw range, in the unit given by symbol.")]
    symbol: Annotated[str | None, Field(description="Unit of the SI values, such as DEG or PCT.")]
    input_mapping: Annotated[
        list[str] | None, Field(description="How a host can use the values: absolute, relative, discrete...")
    ]
    output_mapping: list[str] | None
    motor_bias_percent: Annotated[int | None, Field(description="Power at which a motor starts to turn.")]
    capability_bits: Annotated[str | None, Field(description="The device's 48 capability bits, as hex.")]
    value_format: ValueFormat | None


class PortInfo(BaseModel, frozen=True):
    """What a port's device can do."""

    port: PortName
    port_id: int
    device_type_id: int
    device: str | None
    capabilities: Annotated[list[str], Field(description="output, input, combinable and/or synchronizable.")]
    modes: list[ModeInfo]
    mode_combinations: Annotated[
        list[list[int]] | None, Field(description="Sets of modes the device can report together, if combinable.")
    ]


class PortReading(BaseModel, frozen=True):
    """Values a port's device reported in one mode."""

    port: PortName
    device: str | None
    mode: int
    mode_name: str | None
    values: Annotated[
        list[int | float], Field(description="The values, in the unit given by unit (scaled with the mode's ranges).")
    ]
    unit: Annotated[str | None, Field(description="Unit of values, such as DEG, PCT or MV.")]
    raw_values: Annotated[list[int | float], Field(description="The values as the device sent them.")]


class HubSensors(BaseModel, frozen=True):
    readings: Annotated[list[PortReading], Field(description="One reading per internal sensor, in its first mode.")]


class MotorState(BaseModel, frozen=True):
    """A motor's current state. A field is null when the motor does not report it."""

    port: PortName
    speed_percent: Annotated[int | None, Field(description="Current speed, percent of full speed.")]
    position_degrees: Annotated[
        int | None, Field(description="Encoder position: degrees turned since power-up or the last preset.")
    ]
    absolute_position_degrees: Annotated[
        int | None, Field(description="Angle from the motor's own zero mark, -180 to 179.")
    ]
    load: Annotated[int | None, Field(description="How hard the motor is working, 0-127, as it reports it.")]


class MotorTravel(BaseModel, frozen=True):
    port: PortName
    start_position: Annotated[int | None, Field(description="Encoder position before the move, in degrees.")]
    end_position: Annotated[
        int | None, Field(description="Encoder position after the move, in degrees; null if it didn't wait.")
    ]


class MoveResult(BaseModel, frozen=True):
    """How a motor move ended."""

    outcome: Annotated[
        Literal["started", "completed", "interrupted", "stalled"],
        Field(
            description="started: not waited for. completed: reached its end. interrupted: replaced by another "
            "command. stalled: the motor stopped turning (or ran out of time) before the end, so it was braked."
        ),
    ]
    seconds: Annotated[float | None, Field(description="Duration of the move; null if it didn't wait.")]
    motors: Annotated[list[MotorTravel], Field(description="Positions of each motor that moved.")]
