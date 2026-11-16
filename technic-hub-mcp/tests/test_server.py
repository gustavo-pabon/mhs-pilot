import pytest
from mcp import Client

from technic_hub_mcp import protocol
from technic_hub_mcp.hub import HubError, TechnicHub
from technic_hub_mcp.models import MoveResult
from technic_hub_mcp.protocol import EndState, HubAction
from technic_hub_mcp.server import create_server

pytestmark = pytest.mark.anyio

A, B, AB, LIGHT, TILT = 0, 1, 16, 50, 99
BRAKE, FLOAT, HOLD = EndState.BRAKE, EndState.FLOAT, EndState.HOLD

COMMANDS = [
    ("motor_start_power", {"port": "A", "power": -40}, protocol.start_power(A, -40)),
    ("motor_start_power", {"port": "AB", "power": 30, "second_power": -30}, protocol.start_power_2(AB, 30, -30)),
    ("motor_start_speed", {"port": 1, "speed": 25}, protocol.start_speed(B, 25, 100, 0)),
    (
        "motor_start_speed",
        {"port": "AB", "speed": 20, "second_speed": 40, "max_power": 60, "profile": "both"},
        protocol.start_speed_2(AB, 20, 40, 60, 3),
    ),
    ("motor_run_for_time", {"port": "A", "time_ms": 1500, "speed": 50}, protocol.start_speed_for_time(A, 1500, 50)),
    (
        "motor_run_for_time",
        {
            "port": "AB",
            "time_ms": 800,
            "speed": 10,
            "second_speed": -10,
            "end_state": "hold",
            "profile": "deceleration",
        },
        protocol.start_speed_for_time_2(AB, 800, 10, -10, 100, HOLD, 2),
    ),
    (
        "motor_run_for_degrees",
        {"port": "A", "degrees": 90, "speed": -30, "max_power": 50, "end_state": "float", "profile": "acceleration"},
        protocol.start_speed_for_degrees(A, 90, -30, 50, FLOAT, 1),
    ),
    (
        "motor_run_for_degrees",
        {"port": "AB", "degrees": 360, "speed": 40, "second_speed": 20},
        protocol.start_speed_for_degrees_2(AB, 360, 40, 20),
    ),
    ("motor_go_to_position", {"port": "B", "position": -45, "speed": 30}, protocol.goto_absolute_position(B, -45, 30)),
    (
        "motor_go_to_position",
        {"port": "AB", "position": 90, "second_position": 180, "speed": 20},
        protocol.goto_absolute_position_2(AB, 90, 180, 20),
    ),
    ("motor_stop", {"port": "A"}, protocol.start_power(A, BRAKE)),
    ("motor_stop", {"port": "A", "end_state": "float"}, protocol.start_power(A, FLOAT)),
    ("motor_stop", {"port": "A", "end_state": "hold"}, protocol.start_speed(A, 0)),
    ("motor_set_acceleration_time", {"port": "A", "time_ms": 500}, protocol.set_acceleration_time(A, 500, 0)),
    (
        "motor_set_deceleration_time",
        {"port": "A", "time_ms": 250, "profile_number": 1},
        protocol.set_deceleration_time(A, 250, 1),
    ),
    ("motor_preset_encoder", {"port": "A"}, protocol.preset_encoder(A, 0)),
    (
        "motor_preset_encoder",
        {"port": "AB", "position": 10, "second_position": 20},
        protocol.preset_encoder_2(AB, 10, 20),
    ),
    ("set_led_color", {"color": "green"}, protocol.set_rgb_color_no(LIGHT, 6)),
    ("set_led_rgb", {"red": 255, "green": 128, "blue": 0}, protocol.set_rgb_colors(LIGHT, 255, 128, 0)),
    ("preset_tilt_impact_count", {}, protocol.preset_tilt_impact_count(TILT, 1, 0)),
    ("configure_tilt_impact", {"threshold": 20, "holdoff_ms": 200}, protocol.configure_tilt_impact(TILT, 2, 20, 200)),
]

HUB_ACTIONS = [
    ("set_busy_indication", {"enabled": True}, HubAction.ACTIVATE_BUSY_INDICATION),
    ("set_busy_indication", {"enabled": False}, HubAction.RESET_BUSY_INDICATION),
    ("set_port_vcc", {"enabled": False}, HubAction.VCC_PORT_CONTROL_OFF),
    ("disconnect_hub", {}, HubAction.DISCONNECT),
    ("switch_off_hub", {}, HubAction.SWITCH_OFF),
]

READ_ONLY = {"get_hub_info", "get_hub_alerts", "list_ports", "get_port_info", "read_port_value", "get_hub_sensors"}


class FakeHub(TechnicHub):
    """Records what the tools send instead of using Bluetooth."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []
        self.actions: list[HubAction] = []
        self.error: HubError | None = None

    async def port_id(self, port: str | int) -> int:
        if self.error:
            raise self.error
        return {"A": A, "B": B, "AB": AB}[port] if isinstance(port, str) else port

    def port_name(self, port: int) -> str:
        return {A: "A", B: "B", AB: "AB"}.get(port, str(port))

    def is_virtual(self, port: int) -> bool:
        return port == AB

    async def find_port(self, io_type: int) -> int:
        return {protocol.RGB_LIGHT: LIGHT, protocol.TECHNIC_HUB_TILT_SENSOR: TILT}[io_type]

    async def find_mode(self, port: int, *names: str) -> int | None:
        return {"IMP": 1, "CFG": 2}.get(names[0])

    async def output(self, command: bytes) -> None:
        self.sent.append(command)

    async def write_mode(self, command: bytes) -> None:
        self.sent.append(command)

    async def move(self, command: bytes, *, wait: bool = True) -> MoveResult:
        self.sent.append(command)
        return MoveResult(outcome="completed" if wait else "started", seconds=0.5, motors=[])

    async def hub_action(self, action: HubAction) -> None:
        self.actions.append(action)

    async def disconnect(self) -> None:
        pass


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


async def test_tools_and_their_annotations(hub: FakeHub) -> None:
    async with Client(create_server(hub)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    expected = {name for name, _, _ in COMMANDS + HUB_ACTIONS} | READ_ONLY | {"get_motor_state"}
    expected |= {"set_hub_name", "reset_hub_name", "connect_virtual_port", "disconnect_virtual_port"}
    assert tools.keys() == expected
    assert {name for name, tool in tools.items() if tool.annotations.read_only_hint} == READ_ONLY | {"get_motor_state"}
    assert {name for name, tool in tools.items() if tool.annotations.destructive_hint} == {"switch_off_hub"}
    assert all("\n" not in tool.description for tool in tools.values())


@pytest.mark.parametrize(("tool", "arguments", "command"), COMMANDS)
async def test_command_tools_send_their_command(hub: FakeHub, tool: str, arguments: dict, command: bytes) -> None:
    async with Client(create_server(hub)) as client:
        result = await client.call_tool(tool, arguments)

    assert not result.is_error, result.content[0].text
    assert hub.sent == [command]


@pytest.mark.parametrize(("tool", "arguments", "action"), HUB_ACTIONS)
async def test_action_tools_ask_for_their_action(hub: FakeHub, tool: str, arguments: dict, action: HubAction) -> None:
    async with Client(create_server(hub)) as client:
        result = await client.call_tool(tool, arguments)

    assert not result.is_error
    assert hub.actions == [action]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("motor_start_speed", {"port": "A", "speed": 20, "second_speed": 20}),  # second_* needs a pair
        ("motor_run_for_degrees", {"port": "A", "degrees": 90, "speed": 0}),  # would never finish
        ("motor_run_for_degrees", {"port": "A", "degrees": 90, "speed": 101}),
        ("motor_go_to_position", {"port": "A", "position": 0, "speed": -10}),
        ("set_led_color", {"color": "magenta"}),
        ("set_hub_name", {"name": "A name too long!"}),
        ("connect_virtual_port", {"first_port": "A", "second_port": "A"}),
    ],
)
async def test_rejects_invalid_arguments(hub: FakeHub, tool: str, arguments: dict) -> None:
    async with Client(create_server(hub)) as client:
        result = await client.call_tool(tool, arguments)

    assert result.is_error
    assert hub.sent == []


async def test_reports_hub_errors_to_the_model(hub: FakeHub) -> None:
    hub.error = HubError("LEGO Technic Hub not found.")
    async with Client(create_server(hub)) as client:
        result = await client.call_tool("motor_stop", {"port": "A"})

    assert result.is_error
    assert "LEGO Technic Hub not found." in result.content[0].text
