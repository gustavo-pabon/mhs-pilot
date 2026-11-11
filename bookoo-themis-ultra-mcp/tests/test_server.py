import pytest
from mcp import Client

from bookoo_themis_ultra_mcp import protocol
from bookoo_themis_ultra_mcp.protocol import PowderWeight, ScaleData
from bookoo_themis_ultra_mcp.scale import BookooScale, ScaleError
from bookoo_themis_ultra_mcp.server import create_server

pytestmark = pytest.mark.anyio

READING = ScaleData(
    weight_g=20.1,
    flow_rate_g_per_s=0.05,
    timer_ms=0,
    battery_percent=80,
    display_unit="g",
    auto_off_time_min=15.0,
    beep_level=1,
    flow_smoothing=False,
)

COMMANDS = [
    ("tare", {}, protocol.tare()),
    ("set_beep_level", {"level": 0}, protocol.set_beep_level(0)),
    ("set_auto_off_time", {"minutes": 30}, protocol.set_auto_off_time(30)),
    ("start_timer", {}, protocol.start_timer()),
    ("stop_timer", {}, protocol.stop_timer()),
    ("reset_timer", {}, protocol.reset_timer()),
    ("tare_and_start_timer", {}, protocol.tare_and_start_timer()),
    ("set_flow_smoothing", {"enabled": True}, protocol.set_flow_smoothing(True)),
    ("calibrate", {}, protocol.calibrate()),
    (
        "set_auto_stop_condition",
        {"mode": "timing", "condition": "filter_removed"},
        protocol.set_auto_stop_condition("timing", "filter_removed"),
    ),
    (
        "set_auto_stop_condition_legacy",
        {"condition": "flow_stopped"},
        protocol.set_auto_stop_condition_legacy("flow_stopped"),
    ),
    ("set_powder_weight", {"grams": 18.5}, protocol.set_powder_weight(18.5)),
    ("shut_down", {}, protocol.shut_down()),
    ("reset_auto_off_countdown", {}, protocol.reset_auto_off_countdown()),
]


class FakeScale(BookooScale):
    """Records commands and returns a fixed reading instead of using Bluetooth."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []
        self.error: ScaleError | None = None

    async def connect(self) -> None:
        if self.error:
            raise self.error

    async def disconnect(self) -> None:
        pass

    async def read(self) -> ScaleData:
        await self.connect()
        return READING

    async def send(self, command: bytes) -> None:
        await self.connect()
        self.sent.append(command)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def scale() -> FakeScale:
    return FakeScale()


async def test_one_tool_per_protocol_entry(scale: FakeScale) -> None:
    async with Client(create_server(scale)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    readers = {"get_scale_data", "get_powder_weight", "get_auto_mode_event"}
    assert tools.keys() == readers | {name for name, _, _ in COMMANDS}
    assert all(tools[name].annotations.read_only_hint for name in readers)
    assert {name for name, tool in tools.items() if tool.annotations.destructive_hint} == {"calibrate", "shut_down"}


@pytest.mark.parametrize(("tool", "arguments", "command"), COMMANDS)
async def test_command_tools_send_their_command(scale: FakeScale, tool: str, arguments: dict, command: bytes) -> None:
    async with Client(create_server(scale)) as client:
        result = await client.call_tool(tool, arguments)

    assert not result.is_error
    assert scale.sent == [command]


async def test_get_scale_data(scale: FakeScale) -> None:
    async with Client(create_server(scale)) as client:
        result = await client.call_tool("get_scale_data", {})

    assert result.structured_content == READING.model_dump()


async def test_get_powder_weight(scale: FakeScale) -> None:
    async with Client(create_server(scale)) as client:
        missing = await client.call_tool("get_powder_weight", {})
        scale.powder_weight = PowderWeight(weight_g=18.5)
        reported = await client.call_tool("get_powder_weight", {})

    assert missing.is_error
    assert reported.structured_content == {"weight_g": 18.5}


async def test_get_auto_mode_event_before_any_event(scale: FakeScale) -> None:
    async with Client(create_server(scale)) as client:
        result = await client.call_tool("get_auto_mode_event", {})

    assert result.is_error
    assert "has not reported" in result.content[0].text


async def test_rejects_arguments_outside_the_protocol(scale: FakeScale) -> None:
    async with Client(create_server(scale)) as client:
        result = await client.call_tool("set_beep_level", {"level": 4})

    assert result.is_error
    assert scale.sent == []


async def test_reports_scale_errors_to_the_model(scale: FakeScale) -> None:
    scale.error = ScaleError("BOOKOO Themis Ultra not found.")
    async with Client(create_server(scale)) as client:
        result = await client.call_tool("tare", {})

    assert result.is_error
    assert "BOOKOO Themis Ultra not found." in result.content[0].text
