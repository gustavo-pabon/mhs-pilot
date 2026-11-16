"""MCP server exposing the LEGO Technic Hub's Bluetooth protocol (LWP3) as tools.

The tools cover the hub's properties, alerts and actions, the devices on its ports and their modes, virtual port
pairs, and the port output commands for motors, the hub's light and its tilt sensor.
"""

import argparse
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Annotated, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, protocol
from .hub import HubError, TechnicHub
from .models import HubAlerts, HubInfo, HubSensors, MotorState, MoveResult, Port, PortInfo, PortList, PortReading
from .protocol import EndState, HubAction

T = TypeVar("T")

INSTRUCTIONS = """\
Reads and controls a LEGO Technic Hub (88012) and the motors and sensors on its ports over Bluetooth. Ports A-D are \
the hub's connectors; list_ports shows every device, including the hub's built-in sensors, which tools take by port \
number. Speeds and powers are percentages from -100 to 100, whose sign sets the direction. Motor positions are \
encoder degrees from the zero set with motor_preset_encoder. Moves wait until the motor stops unless wait is false; \
a motor that stops turning before the end of a move is braked and reported as stalled. If the hub is not found, \
ask the user to press its button."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(destructive_hint=False, open_world_hint=False)
IDEMPOTENT = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True, open_world_hint=False)

PortRef = Annotated[
    str | int,
    Field(description='Port letter A-D, a synchronized pair such as "AB", or a port number from list_ports.'),
]
Speed = Annotated[int, Field(ge=-100, le=100, description="Percent of full speed; the sign sets the direction.")]
SecondSpeed = Annotated[
    int | None,
    Field(ge=-100, le=100, description="Only for a pair: the second motor's speed. Omit to run both at speed."),
]
MaxPower = Annotated[int, Field(ge=0, le=100, description="Most power to use, percent; lower it to limit torque.")]
EndStateName = Annotated[
    Literal["brake", "float", "hold"],
    Field(description="How the motor stops: brake, float (coast freely) or hold (keep the position)."),
]
Profile = Annotated[
    Literal["none", "acceleration", "deceleration", "both"],
    Field(description="Ramps to apply, as set with motor_set_acceleration_time and motor_set_deceleration_time."),
]
Wait = Annotated[bool, Field(description="Wait until the move ends; false returns as soon as it starts.")]
Position = Annotated[int, Field(ge=protocol.INT32_MIN, le=protocol.INT32_MAX, description="Encoder degrees.")]
SecondPosition = Annotated[
    int | None,
    Field(ge=protocol.INT32_MIN, le=protocol.INT32_MAX, description="Only for a pair: the second motor's position."),
]
RampTime = Annotated[int, Field(ge=0, le=10000, description="Milliseconds to go between 0 and 100 % speed.")]
ProfileNumber = Annotated[int, Field(ge=0, le=127, description="Profile to set; 0 unless you need several.")]

END_STATES = {"brake": EndState.BRAKE, "float": EndState.FLOAT, "hold": EndState.HOLD}
PROFILES = {
    "none": 0,
    "acceleration": protocol.ACCELERATION_PROFILE,
    "deceleration": protocol.DECELERATION_PROFILE,
    "both": protocol.ACCELERATION_PROFILE | protocol.DECELERATION_PROFILE,
}
IMPACT_COUNT_MODES = ("IMP", "IMPCT")
IMPACT_CONFIG_MODES = ("CFG", "IM_CF")


def create_server(hub: TechnicHub) -> MCPServer:
    """Create an MCP server that reads and controls ``hub``."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await hub.disconnect()

    mcp = MCPServer("technic-hub", version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan)

    async def resolve(port: str | int, *pair_values: int | None) -> int:
        """Return the port ID; values meant for a pair's second motor need a virtual port."""
        port_id = await _reported(hub.port_id(port))
        if any(value is not None for value in pair_values) and not hub.is_virtual(port_id):
            raise ToolError(f"Port {hub.port_name(port_id)} is a single port: second_* values are for pairs only.")
        return port_id

    async def send(port_id: int, command: bytes, done: str) -> str:
        await _reported(hub.output(command))
        return f"Port {hub.port_name(port_id)}: {done}"

    async def tilt_mode(names: tuple[str, ...]) -> tuple[int, int]:
        """Return the port of the hub's tilt sensor and its mode named one of ``names``."""
        port_id = await _reported(hub.find_port(protocol.TECHNIC_HUB_TILT_SENSOR))
        mode = await _reported(hub.find_mode(port_id, *names))
        if mode is None:
            raise ToolError("The hub's tilt sensor has no mode for this.")
        return port_id, mode

    # Hub

    @mcp.tool(annotations=READ_ONLY)
    async def get_hub_info() -> HubInfo:
        """Read the hub's properties: name, firmware and hardware versions, battery, MAC addresses and more."""
        return await _reported(hub.hub_info())

    @mcp.tool(annotations=READ_ONLY)
    async def get_hub_alerts() -> HubAlerts:
        """Check the hub's alerts: low voltage, high current, low signal strength and over-power condition."""
        return await _reported(hub.hub_alerts())

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_hub_name(
        name: Annotated[
            str, Field(min_length=1, max_length=14, pattern=r"^[ -~]+$", description="1-14 printable ASCII characters.")
        ],
    ) -> str:
        """Rename the hub. It keeps the name, and advertises it, until renamed or reset."""
        return f"The hub's name is now {await _reported(hub.set_name(name))!r}."

    @mcp.tool(annotations=IDEMPOTENT)
    async def reset_hub_name() -> str:
        """Restore the hub's default name."""
        return f"The hub's name is now {await _reported(hub.set_name(None))!r}."

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_busy_indication(enabled: bool) -> str:
        """Show or clear the busy indication on the hub's light, which keeps its own color underneath."""
        action = HubAction.ACTIVATE_BUSY_INDICATION if enabled else HubAction.RESET_BUSY_INDICATION
        await _reported(hub.hub_action(action))
        return f"Busy indication {'shown' if enabled else 'cleared'}."

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_port_vcc(enabled: bool) -> str:
        """Switch the supply voltage (VCC) of the hub's ports on or off."""
        await _reported(hub.hub_action(HubAction.VCC_PORT_CONTROL_ON if enabled else HubAction.VCC_PORT_CONTROL_OFF))
        return f"Port supply voltage switched {'on' if enabled else 'off'}."

    @mcp.tool(annotations=ACTION)
    async def disconnect_hub() -> str:
        """End the Bluetooth connection; the hub switches itself off soon after unless a tool reconnects."""
        await _reported(hub.hub_action(HubAction.DISCONNECT))
        return "The hub disconnected. The next tool call reconnects while the hub is still on."

    @mcp.tool(annotations=DESTRUCTIVE)
    async def switch_off_hub() -> str:
        """Switch the hub off, stopping every motor. Only when asked: it must then be switched on by hand."""
        await _reported(hub.hub_action(HubAction.SWITCH_OFF))
        return "The hub is switching off. Press its button to switch it on again."

    # Ports

    @mcp.tool(annotations=READ_ONLY)
    async def list_ports() -> PortList:
        """List the devices on the hub's ports: connectors A-D, built-in sensors and light, and virtual pairs."""
        return await _reported(hub.list_ports())

    @mcp.tool(annotations=READ_ONLY)
    async def get_port_info(port: PortRef) -> PortInfo:
        """Describe a port's device and each of its modes: name, ranges, unit and value format."""
        return await _reported(hub.port_info(await resolve(port)))

    @mcp.tool(annotations=READ_ONLY)
    async def read_port_value(
        port: PortRef,
        mode: Annotated[int | str, Field(description='Mode number or name from get_port_info, such as 2 or "POS".')],
    ) -> PortReading:
        """Switch a port's device to a mode and read its values, such as a motor's position or a sensor reading."""
        port_id = await resolve(port)
        return await _reported(hub.read_value(port_id, await _reported(hub.mode_number(port_id, mode))))

    @mcp.tool(annotations=READ_ONLY)
    async def get_hub_sensors() -> HubSensors:
        """Read the hub's built-in sensors: battery voltage, current, temperatures, tilt, acceleration, rotation."""
        return await _reported(hub.hub_sensors())

    @mcp.tool(annotations=READ_ONLY)
    async def get_motor_state(port: PortRef) -> MotorState:
        """Read a motor's current speed, encoder position, absolute position and load."""
        port_id = await resolve(port)
        if hub.is_virtual(port_id):
            raise ToolError("Read the motors of a pair one at a time, by their own ports.")
        return await _reported(hub.motor_state(port_id))

    @mcp.tool(annotations=IDEMPOTENT)
    async def connect_virtual_port(first_port: PortRef, second_port: PortRef) -> Port:
        """Pair two motor ports into a virtual port, such as "AB", for synchronized motor commands."""
        first, second = await resolve(first_port), await resolve(second_port)
        if first == second:
            raise ToolError("Pick two different ports.")
        return await _reported(hub.connect_virtual_port(first, second))

    @mcp.tool(annotations=IDEMPOTENT)
    async def disconnect_virtual_port(port: PortRef) -> str:
        """Remove a virtual port pair; its two ports stay usable on their own."""
        port_id = await resolve(port)
        name = hub.port_name(port_id)
        await _reported(hub.disconnect_virtual_port(port_id))
        return f"Virtual port {name} removed."

    # Motors

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_start_power(
        port: PortRef,
        power: Annotated[int, Field(ge=-100, le=100, description="Percent power; the sign sets the direction.")],
        second_power: Annotated[
            int | None, Field(ge=-100, le=100, description="Only for a pair: the second motor's power.")
        ] = None,
    ) -> str:
        """Run a motor at an unregulated power until another command; 0 lets it coast."""
        port_id = await resolve(port, second_power)
        if second_power is None:
            command = protocol.start_power(port_id, power)
        else:
            command = protocol.start_power_2(port_id, power, second_power)
        return await send(port_id, command, "running at the given power until another command.")

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_start_speed(
        port: PortRef,
        speed: Speed,
        max_power: MaxPower = 100,
        profile: Profile = "none",
        second_speed: SecondSpeed = None,
    ) -> str:
        """Run a motor at a regulated speed until another command; speed 0 holds its position."""
        port_id = await resolve(port, second_speed)
        use = PROFILES[profile]
        if second_speed is None:
            command = protocol.start_speed(port_id, speed, max_power, use)
        else:
            command = protocol.start_speed_2(port_id, speed, second_speed, max_power, use)
        return await send(port_id, command, "running at the given speed until another command.")

    @mcp.tool(annotations=ACTION)
    async def motor_run_for_time(
        port: PortRef,
        time_ms: Annotated[int, Field(ge=0, le=32767, description="Run time in milliseconds.")],
        speed: Speed,
        max_power: MaxPower = 100,
        end_state: EndStateName = "brake",
        profile: Profile = "none",
        second_speed: SecondSpeed = None,
        wait: Wait = True,
    ) -> MoveResult:
        """Run a motor at a regulated speed for a time, then stop it."""
        port_id = await resolve(port, second_speed)
        args = (max_power, END_STATES[end_state], PROFILES[profile])
        if second_speed is None:
            command = protocol.start_speed_for_time(port_id, time_ms, speed, *args)
        else:
            command = protocol.start_speed_for_time_2(port_id, time_ms, speed, second_speed, *args)
        return await _reported(hub.move(command, wait=wait))

    @mcp.tool(annotations=ACTION)
    async def motor_run_for_degrees(
        port: PortRef,
        degrees: Annotated[int, Field(ge=1, le=10_000_000, description="How far to turn; for a pair, on average.")],
        speed: Speed,
        max_power: MaxPower = 100,
        end_state: EndStateName = "brake",
        profile: Profile = "none",
        second_speed: SecondSpeed = None,
        wait: Wait = True,
    ) -> MoveResult:
        """Turn a motor by a number of degrees; the sign of speed sets the direction."""
        port_id = await resolve(port, second_speed)
        if speed == 0 and not second_speed:
            raise ToolError("speed can't be 0: the motor would never get there.")
        args = (max_power, END_STATES[end_state], PROFILES[profile])
        if second_speed is None:
            command = protocol.start_speed_for_degrees(port_id, degrees, speed, *args)
        else:
            command = protocol.start_speed_for_degrees_2(port_id, degrees, speed, second_speed, *args)
        return await _reported(hub.move(command, wait=wait))

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_go_to_position(
        port: PortRef,
        position: Position,
        speed: Annotated[int, Field(ge=1, le=100, description="Percent of full speed.")],
        max_power: MaxPower = 100,
        end_state: EndStateName = "brake",
        profile: Profile = "none",
        second_position: Annotated[
            int | None,
            Field(
                ge=protocol.INT32_MIN,
                le=protocol.INT32_MAX,
                description="Only for a pair: the second motor's target. This hub moved the second motor by the "
                "first one's distance instead, so position motors one at a time when exact positions matter.",
            ),
        ] = None,
        wait: Wait = True,
    ) -> MoveResult:
        """Turn a motor to an encoder position, taking the direction that gets there."""
        port_id = await resolve(port, second_position)
        args = (speed, max_power, END_STATES[end_state], PROFILES[profile])
        if second_position is None:
            command = protocol.goto_absolute_position(port_id, position, *args)
        else:
            command = protocol.goto_absolute_position_2(port_id, position, second_position, *args)
        return await _reported(hub.move(command, wait=wait))

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_stop(port: PortRef, end_state: EndStateName = "brake") -> str:
        """Stop a motor now, replacing its current command."""
        port_id = await resolve(port)
        if end_state == "hold":
            command = protocol.start_speed(port_id, 0)
        else:
            command = protocol.start_power(port_id, END_STATES[end_state])
        return await send(port_id, command, f"stopped ({end_state}).")

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_set_acceleration_time(port: PortRef, time_ms: RampTime, profile_number: ProfileNumber = 0) -> str:
        """Set the acceleration ramp that commands with profile "acceleration" or "both" use."""
        port_id = await resolve(port)
        command = protocol.set_acceleration_time(port_id, time_ms, profile_number)
        return await send(port_id, command, f"acceleration time set to {time_ms} ms.")

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_set_deceleration_time(port: PortRef, time_ms: RampTime, profile_number: ProfileNumber = 0) -> str:
        """Set the deceleration ramp that commands with profile "deceleration" or "both" use."""
        port_id = await resolve(port)
        command = protocol.set_deceleration_time(port_id, time_ms, profile_number)
        return await send(port_id, command, f"deceleration time set to {time_ms} ms.")

    @mcp.tool(annotations=IDEMPOTENT)
    async def motor_preset_encoder(
        port: PortRef, position: Position = 0, second_position: SecondPosition = None
    ) -> str:
        """Set a motor's encoder to a position, 0 by default, stopping the motor; for a pair, both encoders."""
        port_id = await resolve(port, second_position)
        if second_position is None:
            command = protocol.preset_encoder(port_id, position)
        else:
            command = protocol.preset_encoder_2(port_id, position, second_position)
        return await send(port_id, command, "encoder preset.")

    # The hub's light and tilt sensor

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_led_color(color: protocol.LedColor) -> str:
        """Set the hub's light to one of its 10 colors, or off."""
        port_id = await _reported(hub.find_port(protocol.RGB_LIGHT))
        await _reported(hub.write_mode(protocol.set_rgb_color_no(port_id, protocol.LED_COLORS.index(color))))
        return f"Light set to {color}."

    @mcp.tool(annotations=IDEMPOTENT)
    async def set_led_rgb(
        red: Annotated[int, Field(ge=0, le=255)],
        green: Annotated[int, Field(ge=0, le=255)],
        blue: Annotated[int, Field(ge=0, le=255)],
    ) -> str:
        """Set the hub's light to a mix of red, green and blue."""
        port_id = await _reported(hub.find_port(protocol.RGB_LIGHT))
        await _reported(hub.write_mode(protocol.set_rgb_colors(port_id, red, green, blue)))
        return f"Light set to RGB {red}, {green}, {blue}."

    @mcp.tool(annotations=IDEMPOTENT)
    async def preset_tilt_impact_count(
        count: Annotated[int, Field(ge=0, le=protocol.INT32_MAX, description="New value of the counter.")] = 0,
    ) -> str:
        """Set the counter of impacts (bumps) that the hub's tilt sensor has detected, 0 by default."""
        port_id, mode = await tilt_mode(IMPACT_COUNT_MODES)
        await _reported(hub.output(protocol.preset_tilt_impact_count(port_id, mode, count)))
        return f"Tilt impact count set to {count}."

    @mcp.tool(annotations=IDEMPOTENT)
    async def configure_tilt_impact(
        threshold: Annotated[int, Field(ge=0, le=127, description="How hard a bump must be to count.")],
        holdoff_ms: Annotated[int, Field(ge=10, le=1270, description="Minimum time between two bumps, in ms.")],
    ) -> str:
        """Set how hard a bump must be for the hub's tilt sensor to count it, and the minimum time between bumps."""
        port_id, mode = await tilt_mode(IMPACT_CONFIG_MODES)
        await _reported(hub.output(protocol.configure_tilt_impact(port_id, mode, threshold, holdoff_ms)))
        return f"Tilt impact threshold set to {threshold}, with at least {holdoff_ms} ms between bumps."

    return mcp


async def _reported(operation: Awaitable[T]) -> T:
    """Await a hub operation, reporting a failure to the model as a tool error."""
    try:
        return await operation
    except HubError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    """Run the server over stdio, the transport MCP clients use for local servers."""
    parser = argparse.ArgumentParser(description="MCP server for the LEGO Technic Hub.")
    parser.add_argument(
        "--address",
        help="Bluetooth address of the hub (a UUID on macOS). Default: the first Technic Hub found.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait when searching for or connecting to the hub. Default: 10.",
    )
    args = parser.parse_args()
    create_server(TechnicHub(args.address, args.timeout)).run()
