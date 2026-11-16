# LEGO Technic Hub MCP server

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants read and control a LEGO Technic Hub (88012)
over Bluetooth LE: the motors and sensors on its four ports, its built-in sensors and its light. It implements LEGO's
[Wireless Protocol 3.0](https://lego.github.io/lego-ble-wireless-protocol-docs/) (LWP3), with tools for the hub's
properties, alerts and actions, the devices on its ports and their modes, and the port output commands.

## Requirements

- Python 3.10 or later.
- A computer with Bluetooth LE: macOS, Linux (BlueZ) or Windows 10 or later, through
  [bleak](https://github.com/hbldh/bleak).
- The hub with LEGO's own firmware. Pybricks firmware replaces LWP3, so this server can't talk to a hub that runs it.
- The hub switched on and not connected to another app or device: it accepts one connection at a time. Press its
  button: the light flashes white while the hub waits for a connection and turns solid blue once connected. A hub that
  nothing connects to switches itself off after a short while.

On macOS, the app that starts the server (VS Code, Claude, Terminal…) needs Bluetooth access: **System Settings →
Privacy & Security → Bluetooth**.

## Installation

Install it from [PyPI](https://pypi.org/project/technic-hub-mcp/) into a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install technic-hub-mcp
```

This installs the `technic-hub-mcp` command in `.venv/bin/`. It runs the server over stdio. With
[uv](https://docs.astral.sh/uv/), `uvx technic-hub-mcp` runs it without installing it first.

## Configuration

Add the command to your MCP client, using its absolute path. With uv, use `"command": "uvx"` and
`"args": ["technic-hub-mcp"]` instead.

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "technic-hub": {
      "type": "stdio",
      "command": "/absolute/path/to/.venv/bin/technic-hub-mcp"
    }
  }
}
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "technic-hub": {
      "command": "/absolute/path/to/.venv/bin/technic-hub-mcp"
    }
  }
}
```

Options, passed in `args`:

| Option | Default | Description |
| --- | --- | --- |
| `--address ADDRESS` | First Technic Hub found | Bluetooth address of the hub (a UUID on macOS). Use it when several hubs are nearby. |
| `--timeout SECONDS` | `10` | How long to search for and connect to the hub. |

The server looks for a hub that advertises the LWP3 service as a Technic Hub, and connects on the first tool call. If
the connection drops, for example because the hub switched itself off, the next tool call reconnects once the hub is
on again. When the server stops, it brakes the motors and disconnects.

To try the server without an AI client, open it in the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector .venv/bin/technic-hub-mcp
```

## Tools

Tools take ports as a letter, `A` to `D`, a synchronized pair such as `AB` (see `connect_virtual_port`), or a port
number. The hub's built-in devices only have numbers; on the hub this was tested with, they are 50 (light),
59 (current), 60 (voltage), 61 and 96 (temperature), 97 (accelerometer), 98 (gyro), 99 (tilt) and 100 (gesture).
`list_ports` shows them all.

The hub:

| Tool | Protocol message | Notes |
| --- | --- | --- |
| `get_hub_info` | Hub Properties (`0x01`) | Name, firmware and hardware versions, battery, signal, MAC addresses and the other properties, and what the hub advertised |
| `get_hub_alerts` | Hub Alerts (`0x03`) | Low voltage, high current, low signal strength and over-power condition |
| `set_hub_name(name)` | Hub Properties: Set | 1–14 printable ASCII characters. The hub keeps the name and advertises it |
| `reset_hub_name` | Hub Properties: Reset | Restores the default name |
| `set_busy_indication(enabled)` | Hub Actions `0x05`, `0x06` | Shows or clears the busy indication on the light |
| `set_port_vcc(enabled)` | Hub Actions `0x03`, `0x04` | Switches the supply voltage of the ports |
| `disconnect_hub` | Hub Actions `0x02` | The next tool call reconnects while the hub is still on |
| `switch_off_hub` | Hub Actions `0x01` | Destructive: the hub must then be switched on by hand |

Ports and sensors:

| Tool | Protocol messages | Returns |
| --- | --- | --- |
| `list_ports` | Hub Attached I/O (`0x04`) | Every port with a device: connector, built-in or virtual pair, device type and revisions |
| `get_port_info(port)` | Port Information (`0x21`, `0x43`), Port Mode Information (`0x22`, `0x44`) | The device's capabilities and, per mode: name, raw, percent and SI ranges, unit, mappings and value format |
| `read_port_value(port, mode)` | Port Input Format Setup (`0x41`), Port Value (`0x21`, `0x45`) | The values of one mode, by number or name such as `POS`, scaled to the mode's SI unit |
| `get_hub_sensors` | As above | Voltage, current, temperatures, accelerometer, gyro, tilt and gesture, each in its first mode |
| `get_motor_state(port)` | As above | Speed, encoder position, absolute position and load |
| `connect_virtual_port(first_port, second_port)` | Virtual Port Setup (`0x61`) | The new pair, such as `AB`, for synchronized motor commands |
| `disconnect_virtual_port(port)` | Virtual Port Setup (`0x61`) | |

Motors (Port Output Command, `0x81`). On a pair, the `second_*` parameters give the second motor its own power,
speed or position; without them, both motors get the same value.

| Tool | Sub-commands | Notes |
| --- | --- | --- |
| `motor_start_power(port, power, second_power)` | StartPower (WriteDirectModeData mode 0), `0x02` | Unregulated, until another command |
| `motor_start_speed(port, speed, ...)` | StartSpeed `0x07`, `0x08` | Regulated, until another command; speed 0 holds the position |
| `motor_run_for_time(port, time_ms, speed, ...)` | StartSpeedForTime `0x09`, `0x0A` | Up to 32,767 ms |
| `motor_run_for_degrees(port, degrees, speed, ...)` | StartSpeedForDegrees `0x0B`, `0x0C` | The sign of speed sets the direction |
| `motor_go_to_position(port, position, speed, ...)` | GotoAbsolutePosition `0x0D`, `0x0E` | Encoder degrees |
| `motor_stop(port, end_state)` | StartPower brake or float, StartSpeed 0 to hold | Replaces the current command |
| `motor_set_acceleration_time(port, time_ms)` | SetAccTime `0x05` | Ramp for commands with `profile` `acceleration` or `both` |
| `motor_set_deceleration_time(port, time_ms)` | SetDecTime `0x06` | Ramp for commands with `profile` `deceleration` or `both` |
| `motor_preset_encoder(port, position, second_position)` | PresetEncoder (WriteDirectModeData mode 2), `0x14` | Stops the motor |

The run, turn and go-to tools also take `max_power` (0–100 %, to limit torque), `end_state` (`brake`, `float` or
`hold`), `profile` and `wait`. Speeds and powers are percentages from −100 to 100.

The light and the tilt sensor:

| Tool | Command | Notes |
| --- | --- | --- |
| `set_led_color(color)` | SetRgbColorNo | `off`, `pink`, `purple`, `blue`, `light_blue`, `cyan`, `green`, `yellow`, `orange`, `red` or `white` |
| `set_led_rgb(red, green, blue)` | SetRgbColors | 0–255 each |
| `preset_tilt_impact_count(count)` | TiltImpactPreset | Sets the counter of bumps the hub has detected |
| `configure_tilt_impact(threshold, holdoff_ms)` | TiltConfigImpact | How hard a bump must be to count, and the minimum time between two |

## How moves end

`motor_run_for_time`, `motor_run_for_degrees` and `motor_go_to_position` wait until the motors stop, unless `wait` is
false, and return the outcome with each motor's encoder positions before and after:

- `completed`: the move ended as asked.
- `interrupted`: another command replaced it, such as `motor_stop`.
- `stalled`: a motor stopped turning (less than 2° in 1 s) before the end, so the server braked it. A motor that is
  slowed but still turning is not stalled. A mechanism driven against a stop on purpose, to find its zero, ends this
  way.
- `started`: `wait` was false.

The hub reports when most commands finish, but this one (firmware 1.0.00.0000) doesn't for timed runs or for the
degree moves of a pair. For those, the server takes the move as completed once its time is up, or once every motor is
within 5° of its target.

## Good to know

- Values are read on request: `read_port_value` and the other readers put the device in the mode they need, then ask
  for its value. They scale the device's raw values to its SI unit with the ranges the device reports.
- After a move that ends holding its position, the hub sometimes ignores the next command without replying. If the
  hub doesn't acknowledge a motor command within 0.5 s, the server sends it once more.
- For a pair, `motor_go_to_position` with `second_position` didn't do what the protocol describes on the hub this was
  tested with: the second motor moved by the first motor's distance instead of to its own target. Position motors one
  at a time when exact positions matter.
- Some hub properties may be missing from `get_hub_info`: the tested hub doesn't report its hardware network family.
- Tool annotations mark the reading tools as read-only and `switch_off_hub` as destructive, so clients can ask before
  running it.
- Tested on a Technic Hub with firmware 1.0.00.0000 and four Technic Large Motors (88013), from macOS: every reading
  tool; `set_led_color`, `set_led_rgb` and `set_busy_indication`; `disconnect_hub`, with the reconnection that
  follows, and `switch_off_hub`; and all the motor tools on single motors, plus pairs with `connect_virtual_port`,
  `disconnect_virtual_port`, `motor_run_for_degrees`, `motor_run_for_time` and `motor_go_to_position`. Not tried on a
  hub yet: `set_hub_name`, `reset_hub_name`, `set_port_vcc`, `preset_tilt_impact_count`, `configure_tilt_impact`,
  `motor_stop` with `hold`, and the `second_power`, `second_speed` and `second_position` values of
  `motor_start_power`, `motor_start_speed` and `motor_preset_encoder`.

## Not covered

These parts of the protocol are left out on purpose:

- Firmware update and memory lock (`0x10`–`0x13`), and the production-only fast shutdown (Hub Actions `0x2F`): they can
  leave the hub unusable.
- Raw WriteDirect (`0x50`), which carries the hardware reset and the tilt sensor's factory calibration: they can
  de-calibrate a device.
- Hardware network commands (`0x08`) and setting the hardware network ID and family: they are for hubs connected to
  each other without a computer.
- Value notifications and combined-mode sensor streams (`0x42`, `0x46`, `0x48`): the tools read values on request
  instead.
- Queued commands: every output command runs immediately, replacing the port's current command.

## Python library

The protocol and Bluetooth layers also work without MCP:

```python
import asyncio

from technic_hub_mcp import protocol
from technic_hub_mcp.hub import TechnicHub


async def main() -> None:
    hub = TechnicHub()
    try:
        motor = await hub.port_id("A")
        result = await hub.move(protocol.start_speed_for_degrees(motor, 90, 30))
        print(result.outcome, result.motors[0].end_position)
    finally:
        await hub.disconnect()


asyncio.run(main())
```

- [`protocol`](https://github.com/gustavo-pabon/mhs-pilot/blob/main/technic-hub-mcp/src/technic_hub_mcp/protocol.py):
  builds and decodes LWP3 messages. No Bluetooth.
- [`hub`](https://github.com/gustavo-pabon/mhs-pilot/blob/main/technic-hub-mcp/src/technic_hub_mcp/hub.py):
  `TechnicHub` finds, connects to and reconnects to the hub, matches replies to requests and follows motor commands.
- [`models`](https://github.com/gustavo-pabon/mhs-pilot/blob/main/technic-hub-mcp/src/technic_hub_mcp/models.py):
  the results the tools return.
- [`server`](https://github.com/gustavo-pabon/mhs-pilot/blob/main/technic-hub-mcp/src/technic_hub_mcp/server.py):
  the MCP tools.

## Development

```bash
git clone https://github.com/gustavo-pabon/mhs-pilot.git
cd mhs-pilot/technic-hub-mcp
python3 -m venv .venv
.venv/bin/pip install -e . --group dev   # needs pip 25.1 or later
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests don't need a hub: they use a simulated one.

To release, update `__version__` in `src/technic_hub_mcp/__init__.py` through a pull request. Once it's merged, tag
that commit on `main` with the new version, for example `technic-hub-mcp-v0.1.1`, and push the tag. GitHub Actions
then tests the package on Python 3.10–3.14, builds it, and publishes it to PyPI after a maintainer approves the
deployment on the workflow run.

## License

[MIT](https://github.com/gustavo-pabon/mhs-pilot/blob/main/technic-hub-mcp/LICENSE). This project is not affiliated
with or endorsed by the LEGO Group. LEGO and Technic are trademarks of the LEGO Group.
