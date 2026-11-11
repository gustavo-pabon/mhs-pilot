"""MCP server exposing the BOOKOO Themis Ultra Bluetooth protocol as tools.

There is one tool per entry in BOOKOO's official protocol: three that read the data
packets the scale sends, and one for each command.
"""

import argparse
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Annotated, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, protocol
from .protocol import AutoMode, AutoModeEvent, LegacyStopCondition, PowderWeight, ScaleData, StopCondition
from .scale import BookooScale, ScaleError

T = TypeVar("T")

INSTRUCTIONS = """\
Reads and controls a BOOKOO Themis Ultra scale over Bluetooth. Weights are in grams. \
The scale does not acknowledge commands: call get_scale_data afterwards to check their \
effect. Timer commands only work in the scale's timing and ratio modes, and some tools \
need a minimum firmware version, given in their descriptions."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(destructive_hint=False, open_world_hint=False)
IDEMPOTENT = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True, open_world_hint=False)


def create_server(scale: BookooScale) -> MCPServer:
    """Create an MCP server that reads and controls ``scale``."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await scale.disconnect()

    mcp = MCPServer("bookoo-themis-ultra", version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan)

    async def send(command: bytes) -> str:
        await _reported(scale.send(command))
        return "Command sent."

    # Data packets

    @mcp.tool(annotations=READ_ONLY)
    async def get_scale_data() -> ScaleData:
        """Read the scale's live data: weight, flow rate, timer, battery level and settings."""
        return await _reported(scale.read())

    @mcp.tool(annotations=READ_ONLY)
    async def get_powder_weight() -> PowderWeight:
        """Get the last powder (dose) weight the scale reported. Firmware V3.2.4 beta or V4.0.0+."""
        await _reported(scale.connect())
        if scale.powder_weight is None:
            raise ToolError("The scale has not reported a powder weight since the server connected.")
        return scale.powder_weight

    @mcp.tool(annotations=READ_ONLY)
    async def get_auto_mode_event() -> AutoModeEvent:
        """Get the last automatic-mode event and its settlement data. Firmware V3.2.4 beta or V4.0.0+."""
        await _reported(scale.connect())
        if scale.auto_mode_event is None:
            raise ToolError("The scale has not reported an automatic-mode event since the server connected.")
        return scale.auto_mode_event

    # Commands

    @mcp.tool(annotations=ACTION)
    async def tare() -> str:
        """Zero the scale. Not available while charging, or while automatic mode is running or done."""
        return await send(protocol.tare())

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_beep_level(
        level: Annotated[int, Field(ge=0, le=3, description="0 = mute, 1 = low, 2 = medium, 3 = high.")],
    ) -> str:
        """Set the beep volume."""
        return await send(protocol.set_beep_level(level))

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_auto_off_time(minutes: Annotated[int, Field(ge=5, le=30)]) -> str:
        """Set how many idle minutes the scale waits before turning itself off."""
        return await send(protocol.set_auto_off_time(minutes))

    @mcp.tool(annotations=ACTION)
    async def start_timer() -> str:
        """Start the timer. Only works in timing and ratio modes."""
        return await send(protocol.start_timer())

    @mcp.tool(annotations=ACTION)
    async def stop_timer() -> str:
        """Stop the timer. Only works in timing and ratio modes."""
        return await send(protocol.stop_timer())

    @mcp.tool(annotations=IDEMPOTENT)
    async def reset_timer() -> str:
        """Reset the timer to zero. Only works in timing and ratio modes."""
        return await send(protocol.reset_timer())

    @mcp.tool(annotations=ACTION)
    async def tare_and_start_timer() -> str:
        """Tare, reset and start the timer; in automatic mode, start extraction when the scale is ready."""
        return await send(protocol.tare_and_start_timer())

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_flow_smoothing(enabled: bool) -> str:
        """Turn flow-rate smoothing on or off."""
        return await send(protocol.set_flow_smoothing(enabled))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def calibrate() -> str:
        """Start calibration. Only when the user asks: a wrong calibration makes every later reading wrong."""
        return await send(protocol.calibrate())

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_auto_stop_condition(
        mode: AutoMode,
        condition: Annotated[
            StopCondition,
            Field(description="flow_stopped, or the filter or server being lifted off the scale."),
        ],
    ) -> str:
        """Set what ends an automatic brew. Firmware V4.0.0+; on older firmware use the legacy tool."""
        return await send(protocol.set_auto_stop_condition(mode, condition))

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_auto_stop_condition_legacy(
        condition: Annotated[
            LegacyStopCondition,
            Field(description="flow_stopped, or the container being lifted off the scale."),
        ],
    ) -> str:
        """Set what ends an automatic brew. Firmware V3.1.2 and earlier; on V4.0.0+ use set_auto_stop_condition."""
        return await send(protocol.set_auto_stop_condition_legacy(condition))

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_powder_weight(
        grams: Annotated[float, Field(ge=0.1, le=999.9, description="Rounded to 0.1 g.")],
    ) -> str:
        """Set the powder (dose) weight used by ratio mode. Firmware V3.2.4 beta or V4.0.0+."""
        return await send(protocol.set_powder_weight(grams))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def shut_down() -> str:
        """Turn the scale off; it must then be switched on by hand. Firmware V4.0.0+, not while charging."""
        return await send(protocol.shut_down())

    @mcp.tool(annotations=IDEMPOTENT)
    async def reset_auto_off_countdown() -> str:
        """Restart the auto-off countdown so the scale stays on. Firmware V4.0.1+."""
        return await send(protocol.reset_auto_off_countdown())

    return mcp


async def _reported(operation: Awaitable[T]) -> T:
    """Await a scale operation, reporting a failure to the model as a tool error."""
    try:
        return await operation
    except ScaleError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    """Run the server over stdio, the transport MCP clients use for local servers."""
    parser = argparse.ArgumentParser(description="MCP server for the BOOKOO Themis Ultra scale.")
    parser.add_argument(
        "--address",
        help="Bluetooth address of the scale (a UUID on macOS). Default: the first Themis Ultra found.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait when searching for, connecting to or reading from the scale. Default: 10.",
    )
    args = parser.parse_args()
    create_server(BookooScale(args.address, args.timeout)).run()
