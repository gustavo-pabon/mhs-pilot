import asyncio
import struct
from dataclasses import dataclass, field

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from technic_hub_mcp import hub as hub_module
from technic_hub_mcp import protocol
from technic_hub_mcp.hub import HubError, RejectedError, TechnicHub

pytestmark = pytest.mark.anyio

ADDRESS = "4E1D1E6B-0000-0000-0000-000000000000"
ADVERTISEMENT = AdvertisementData(
    "Technic Hub", {protocol.LEGO_COMPANY_ID: bytes.fromhex("008006004100")}, {}, [protocol.SERVICE_UUID], None, -50, ()
)
OVERSHOOT = 5


@dataclass
class Mode:
    name: str
    value_format: bytes  # number of values, type, digits, decimals
    raw: tuple[float, float] = (0.0, 0.0)
    si: tuple[float, float] = (0.0, 0.0)
    symbol: str = ""


@dataclass
class Device:
    io_type: int
    modes: list[Mode]
    input_modes: int
    output_modes: int
    values: dict[int, list[int]] = field(default_factory=dict)
    mode: int = 0
    position: int = 0

    def read(self) -> bytes:
        values = {1: [0], 2: [self.position], 3: [(self.position + 180) % 360 - 180]} if self.is_motor else self.values
        code = protocol.DATASET_TYPES[self.modes[self.mode].value_format[1]][1]
        return struct.pack(f"<{len(values[self.mode])}{code}", *values[self.mode])

    @property
    def is_motor(self) -> bool:
        return self.io_type in protocol.MOTORS


def motor() -> Device:
    modes = [
        Mode("POWER", bytes([1, 0, 4, 0]), (-100, 100), (-100, 100), "PCT"),
        Mode("SPEED", bytes([1, 0, 4, 0]), (-100, 100), (-100, 100), "PCT"),
        Mode("POS", bytes([1, 2, 4, 0]), (-360, 360), (-360, 360), "DEG"),
        Mode("APOS", bytes([1, 1, 3, 0]), (-180, 179), (-180, 179), "DEG"),
    ]
    return Device(0x2E, modes, input_modes=0b1110, output_modes=0b0001)


class SimulatedHub:
    """A Technic Hub as bleak sees it: it answers requests and runs motor commands, without Bluetooth."""

    def __init__(self) -> None:
        self.devices = {
            0: motor(),
            1: motor(),
            50: Device(0x17, [Mode("COL O", bytes([1, 0, 3, 0])), Mode("RGB O", bytes([3, 0, 3, 0]))], 0, 0b11),
            96: Device(0x3C, [Mode("TEMP", bytes([1, 1, 5, 1]), (-900, 900), (-90, 90), "DEG")], 1, 0, {0: [245]}),
        }
        self.properties = {0x01: b"Technic Hub", 0x03: bytes.fromhex("00000010"), 0x06: bytes([87]), 0x0B: b"\x80"}
        self.pairs: dict[int, tuple[int, int]] = {}
        self.written: list[bytes] = []
        self.ignore_next: set[int] = set()  # ports whose next command the hub silently drops
        self.blocked: set[int] = set()  # motors that can't turn
        self.unreadable: set[int] = set()  # ports whose values the hub won't report
        self.move_time = 0.1  # seconds a degrees move takes
        self.busy: set[int] = set()
        self.is_connected = False
        self._moves: dict[int, asyncio.TimerHandle] = {}

    # The parts of BleakScanner and BleakClient the hub uses

    async def find_device_by_filter(self, matches, timeout: float) -> BLEDevice | None:
        device = BLEDevice(ADDRESS, "Technic Hub", None)
        return device if matches(device, ADVERTISEMENT) else None

    def client(self, device: BLEDevice, disconnected_callback, **_) -> "SimulatedHub":
        self._disconnected_callback = disconnected_callback
        return self

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, uuid: str, callback) -> None:
        self._notify = callback
        for port, device in self.devices.items():
            self.send(
                protocol.MessageType.HUB_ATTACHED_IO, bytes([port, 1]) + struct.pack("<H", device.io_type) + bytes(8)
            )

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        if not self.is_connected:
            raise BleakError("Not connected")
        assert response
        self.written.append(bytes(data))
        self._handle(data[2], data[3:])

    # Simulation

    def drop_connection(self) -> None:
        self.is_connected = False
        self._disconnected_callback(self)

    def send(self, kind: int, payload: bytes, delay: float = 0.01) -> None:
        asyncio.get_running_loop().call_later(delay, self._notify, None, bytearray(protocol.message(kind, payload)))

    def _handle(self, kind: int, body: bytes) -> None:
        if kind == protocol.MessageType.HUB_PROPERTIES and body[1] == protocol.PropertyOperation.REQUEST_UPDATE:
            if body[0] in self.properties:
                self.send(kind, bytes([body[0], 0x06]) + self.properties[body[0]])
            else:
                self.send(protocol.MessageType.GENERIC_ERROR, bytes([kind, 0x05]))
        elif kind == protocol.MessageType.HUB_ALERTS:
            self.send(kind, bytes([body[0], protocol.ALERT_UPDATE, 0x00]))
        elif kind == protocol.MessageType.HUB_ACTIONS and body[0] == protocol.HubAction.SWITCH_OFF:
            self.send(kind, bytes([protocol.HubAction.WILL_SWITCH_OFF]))
            asyncio.get_running_loop().call_later(0.02, self.drop_connection)
        elif kind == protocol.MessageType.PORT_INFORMATION_REQUEST:
            device = self.devices[body[0]]
            if body[1] == protocol.PortInformationType.PORT_VALUE and body[0] in self.unreadable:
                self.send(protocol.MessageType.GENERIC_ERROR, bytes([kind, 0x06]))
            elif body[1] == protocol.PortInformationType.PORT_VALUE:
                self.send(protocol.MessageType.PORT_VALUE, bytes([body[0]]) + device.read())
            elif body[1] == protocol.PortInformationType.POSSIBLE_MODE_COMBINATIONS:
                self.send(protocol.MessageType.PORT_INFORMATION, bytes([body[0], 0x02, 0b0110, 0, 0, 0]))
            else:
                info = struct.pack("<BBHH", 0x0F, len(device.modes), device.input_modes, device.output_modes)
                self.send(protocol.MessageType.PORT_INFORMATION, bytes([body[0], 0x01]) + info)
        elif kind == protocol.MessageType.PORT_MODE_INFORMATION_REQUEST:
            self._mode_information(*body[:3])
        elif kind == protocol.MessageType.PORT_INPUT_FORMAT_SETUP:
            self.devices[body[0]].mode = body[1]
            self.send(protocol.MessageType.PORT_INPUT_FORMAT, body[:7])
        elif kind == protocol.MessageType.VIRTUAL_PORT_SETUP and body[0] == 0x01:
            self.pairs[0x10] = (body[1], body[2])
            self.send(protocol.MessageType.HUB_ATTACHED_IO, bytes([0x10, 0x02, 0x2E, 0x00, body[1], body[2]]))
        elif kind == protocol.MessageType.PORT_OUTPUT_COMMAND:
            self._output(body[0], body[2], body[3:])

    def _mode_information(self, port: int, mode_number: int, info: int) -> None:
        mode = self.devices[port].modes[mode_number]
        payloads = {
            0x00: mode.name.encode(),
            0x01: struct.pack("<ff", *mode.raw),
            0x02: struct.pack("<ff", -100, 100),
            0x03: struct.pack("<ff", *mode.si),
            0x04: mode.symbol.encode(),
            0x05: bytes([0x10, 0x00]),
            0x80: mode.value_format,
        }
        if info in payloads:
            self.send(protocol.MessageType.PORT_MODE_INFORMATION, bytes([port, mode_number, info]) + payloads[info])
        else:
            self.send(
                protocol.MessageType.GENERIC_ERROR, bytes([protocol.MessageType.PORT_MODE_INFORMATION_REQUEST, 6])
            )

    def _output(self, port: int, sub_command: int, payload: bytes) -> None:
        if port in self.ignore_next:
            self.ignore_next.discard(port)
            return
        motors = [self.devices[motor] for motor in self.pairs.get(port, (port,))]
        if not all(motor.is_motor for motor in motors):
            self.send(protocol.MessageType.GENERIC_ERROR, bytes([protocol.MessageType.PORT_OUTPUT_COMMAND, 0x06]))
            return
        discarded = protocol.Feedback.DISCARDED if port in self.busy else 0
        self.busy.discard(port)
        if move := self._moves.pop(port, None):
            move.cancel()
        if sub_command == protocol.WRITE_DIRECT_MODE_DATA:  # power or encoder preset: done at once
            if payload[0] == 0x02:
                for motor in motors:
                    motor.position = struct.unpack_from("<i", payload, 1)[0]
            self._feedback(port, protocol.Feedback.IDLE | protocol.Feedback.COMPLETED | discarded)
            return
        self.busy.add(port)
        self._feedback(port, protocol.Feedback.IN_PROGRESS | discarded)
        if port in self.blocked:
            return
        loop = asyncio.get_running_loop()
        # Like the real hub, never report timed runs and paired moves as completed
        if sub_command == 0x09:
            time_ms, speed = struct.unpack_from("<hb", payload)
            loop.call_later(0.05, setattr, motors[0], "position", motors[0].position + speed * time_ms // 100)
        elif sub_command == 0x0C:
            degrees, *speeds = struct.unpack_from("<ibb", payload)
            for motor, speed in zip(motors, speeds):
                travel = round(2 * degrees * speed / sum(map(abs, speeds))) + OVERSHOOT * (1 if speed > 0 else -1)
                loop.call_later(0.05, setattr, motor, "position", motor.position + travel)
        elif sub_command == 0x0B:
            degrees, speed = struct.unpack_from("<ib", payload)
            target = motors[0].position + (degrees + OVERSHOOT) * (1 if speed > 0 else -1)
            self._moves[port] = loop.call_later(self.move_time, self._finish, port, target)

    def _finish(self, port: int, position: int) -> None:
        del self._moves[port]
        self.busy.discard(port)
        self.devices[port].position = position
        self._feedback(port, protocol.Feedback.IDLE | protocol.Feedback.COMPLETED)

    def _feedback(self, port: int, flags: int) -> None:
        self.send(protocol.MessageType.PORT_OUTPUT_COMMAND_FEEDBACK, bytes([port, flags]))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> SimulatedHub:
    sim = SimulatedHub()
    monkeypatch.setattr(hub_module, "BleakScanner", sim)
    monkeypatch.setattr(hub_module, "BleakClient", sim.client)
    for name, value in {
        "COMMAND_TIMEOUT": 0.1,
        "POLL_INTERVAL": 0.05,
        "STALL_TIME": 0.3,
        "STOPPED_TIME": 0.1,
        "SETTLE_TIME": 0,
    }.items():
        monkeypatch.setattr(hub_module, name, value)
    return sim


def degrees(port: int, angle: int, speed: int = 50) -> bytes:
    return protocol.start_speed_for_degrees(port, angle, speed)


def sent_to(sim: SimulatedHub, port: int) -> list[bytes]:
    return [m for m in sim.written if m[2] == protocol.MessageType.PORT_OUTPUT_COMMAND and m[3] == port]


async def started_moving(hub: TechnicHub, sim: SimulatedHub, command: bytes) -> asyncio.Task:
    """Start a move and return once the hub has acknowledged its command."""
    move = asyncio.create_task(hub.move(command))
    while command not in sim.written or 0 in sim.ignore_next:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    return move


async def test_lists_and_resolves_ports(sim: SimulatedHub) -> None:
    hub = TechnicHub()

    ports = (await hub.list_ports()).ports

    assert [(p.port, p.kind, p.device) for p in ports] == [
        ("A", "connector", "Technic Large Motor"),
        ("B", "connector", "Technic Large Motor"),
        ("50", "internal", "RGB Light"),
        ("96", "internal", "Technic Hub Temperature Sensor"),
    ]
    assert [await hub.port_id(port) for port in ("a", 1, "96", 96)] == [0, 1, 96, 96]
    with pytest.raises(HubError, match="Nothing is attached to port 'C'"):
        await hub.port_id("C")
    with pytest.raises(HubError, match="connect_virtual_port"):
        await hub.port_id("AB")


async def test_reads_hub_info(sim: SimulatedHub) -> None:
    info = await TechnicHub().hub_info()

    assert (info.name, info.firmware_version, info.battery_percent, info.system_type) == (
        "Technic Hub",
        "1.0.00.0000",
        87,
        "Technic Hub",
    )
    assert info.address == ADDRESS
    assert info.manufacturer is None  # the hub rejected the request
    assert info.advertisement.capabilities == ["peripheral_role", "lpf2_devices"]


async def test_reads_values_scaled_to_si_units(sim: SimulatedHub) -> None:
    hub = TechnicHub()

    reading = await hub.read_value(96, 0)
    await hub.read_value(96, 0)

    assert (reading.mode_name, reading.values, reading.unit, reading.raw_values) == ("TEMP", [24.5], "DEG", [245])
    setups = [m for m in sim.written if m[2] == protocol.MessageType.PORT_INPUT_FORMAT_SETUP]
    assert setups == [protocol.set_port_input_format(96, 0)]  # the mode is set once
    with pytest.raises(HubError, match="doesn't report values"):
        await hub.read_value(50, 0)


async def test_describes_port_modes(sim: SimulatedHub) -> None:
    info = await TechnicHub().port_info(0)

    assert [(m.name, m.input, m.output) for m in info.modes] == [
        ("POWER", False, True),
        ("SPEED", True, False),
        ("POS", True, False),
        ("APOS", True, False),
    ]
    position = info.modes[2]
    assert (position.si_range.max, position.symbol, position.value_format.dataset_type) == (360, "DEG", "int32")
    assert position.motor_bias_percent is None  # the hub rejected the request
    assert info.mode_combinations == [[1, 2]]


async def test_moves_and_reports_positions(sim: SimulatedHub) -> None:
    result = await TechnicHub().move(degrees(0, 90))

    assert result.outcome == "completed"
    assert [(m.port, m.start_position, m.end_position) for m in result.motors] == [("A", 0, 90 + OVERSHOOT)]


async def test_resends_a_command_the_hub_ignores(sim: SimulatedHub) -> None:
    sim.ignore_next.add(0)

    result = await TechnicHub().move(degrees(0, 90))

    assert result.outcome == "completed"
    assert sent_to(sim, 0) == [degrees(0, 90)] * 2


async def test_gives_up_when_the_hub_ignores_a_command_twice(sim: SimulatedHub) -> None:
    hub = TechnicHub()
    await hub.connect()
    sim.ignore_next.add(0)
    original = sim._output

    def ignore_twice(port: int, sub_command: int, payload: bytes) -> None:
        if len(sent_to(sim, 0)) > 1:
            original(port, sub_command, payload)

    sim._output = ignore_twice

    with pytest.raises(HubError, match="ignored the command for port A twice"):
        await hub.output(protocol.start_speed(0, 20))


async def test_brakes_a_stalled_motor(sim: SimulatedHub) -> None:
    sim.blocked.add(0)

    result = await TechnicHub().move(degrees(0, 90))

    assert result.outcome == "stalled"
    assert sent_to(sim, 0)[-1] == protocol.start_power(0, protocol.EndState.BRAKE)


async def test_a_motor_whose_position_cannot_be_read_is_not_taken_for_stalled(sim: SimulatedHub) -> None:
    sim.move_time = 0.6
    move = await started_moving(TechnicHub(), sim, degrees(0, 90))

    sim.unreadable.add(0)

    assert (await move).outcome == "completed"
    assert protocol.start_power(0, protocol.EndState.BRAKE) not in sim.written


async def test_brakes_a_motor_whose_position_stays_unreadable(
    sim: SimulatedHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "UNTRACKED_MOVE_TIMEOUT", 0.5)
    sim.blocked.add(0)
    move = await started_moving(TechnicHub(), sim, degrees(0, 90))

    sim.unreadable.add(0)

    assert (await move).outcome == "stalled"
    assert sent_to(sim, 0)[-1] == protocol.start_power(0, protocol.EndState.BRAKE)


async def test_a_timed_run_completes_once_its_time_is_up(sim: SimulatedHub) -> None:
    result = await TechnicHub().move(protocol.start_speed_for_time(0, 200, 50))

    assert result.outcome == "completed"
    assert result.motors[0].end_position == 100
    assert protocol.start_power(0, protocol.EndState.BRAKE) not in sim.written


async def test_a_timed_hold_completes_without_turning(sim: SimulatedHub) -> None:
    result = await TechnicHub().move(protocol.start_speed_for_time(0, 200, 0))

    assert result.outcome == "completed"


async def test_a_timed_run_that_cannot_turn_stalls(sim: SimulatedHub) -> None:
    sim.blocked.add(0)

    result = await TechnicHub().move(protocol.start_speed_for_time(0, 200, 50))

    assert result.outcome == "stalled"
    assert sent_to(sim, 0)[-1] == protocol.start_power(0, protocol.EndState.BRAKE)


async def test_a_paired_move_completes_once_both_motors_get_there(sim: SimulatedHub) -> None:
    hub = TechnicHub()
    await hub.connect()
    pair = await hub.connect_virtual_port(0, 1)

    result = await hub.move(protocol.start_speed_for_degrees_2(pair.port_id, 90, 30, -30))

    assert result.outcome == "completed"
    assert [(m.port, m.end_position) for m in result.motors] == [("A", 90 + OVERSHOOT), ("B", -90 - OVERSHOOT)]


async def test_reports_a_move_another_command_interrupts(sim: SimulatedHub) -> None:
    sim.blocked.add(0)
    hub = TechnicHub()
    move = await started_moving(hub, sim, degrees(0, 90))

    await hub.output(protocol.start_power(0, protocol.EndState.BRAKE))

    assert (await move).outcome == "interrupted"


async def test_reports_commands_the_hub_rejects(sim: SimulatedHub) -> None:
    with pytest.raises(RejectedError, match="invalid use"):
        await TechnicHub().output(protocol.start_speed(50, 20))


async def test_fails_a_move_when_the_hub_disconnects(sim: SimulatedHub) -> None:
    sim.blocked.add(0)
    move = await started_moving(TechnicHub(), sim, degrees(0, 90))

    sim.drop_connection()

    with pytest.raises(HubError):
        await move
    assert protocol.start_power(0, protocol.EndState.BRAKE) not in sim.written


async def test_pairs_ports(sim: SimulatedHub) -> None:
    hub = TechnicHub()
    await hub.connect()

    pair = await hub.connect_virtual_port(0, 1)

    assert (pair.port, pair.kind, pair.pair) == ("AB", "virtual", ["A", "B"])
    assert await hub.port_id("ab") == pair.port_id


async def test_switching_off_ends_the_connection(sim: SimulatedHub) -> None:
    hub = TechnicHub()

    await hub.hub_action(protocol.HubAction.SWITCH_OFF)
    await asyncio.sleep(0.05)

    assert not hub.is_connected
