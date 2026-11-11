# BOOKOO Themis Ultra MCP server

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants read and control a BOOKOO Themis Ultra
scale over Bluetooth LE. It covers every data packet and command in BOOKOO's
[official Bluetooth protocol](https://github.com/BooKooCode/OpenSource/blob/main/bookoo_ultra_scale/protocols.md),
with one tool per protocol entry.

## Requirements

- Python 3.10 or later.
- A computer with Bluetooth LE: macOS, Linux (BlueZ) or Windows 10 or later, through
  [bleak](https://github.com/hbldh/bleak).
- The scale switched on, in range and not connected to another app, such as the BOOKOO app. It accepts one
  connection at a time.

On macOS, the app that starts the server (VS Code, Claude, Terminal…) needs Bluetooth access: **System Settings →
Privacy & Security → Bluetooth**.

## Installation

From this folder:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

This installs the `bookoo-themis-ultra-mcp` command in `.venv/bin/`. It runs the server over stdio.

## Configuration

Add the command to your MCP client, using its absolute path.

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "bookoo-themis-ultra": {
      "type": "stdio",
      "command": "/absolute/path/to/bookoo-themis-ultra-mcp/.venv/bin/bookoo-themis-ultra-mcp"
    }
  }
}
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bookoo-themis-ultra": {
      "command": "/absolute/path/to/bookoo-themis-ultra-mcp/.venv/bin/bookoo-themis-ultra-mcp"
    }
  }
}
```

Options, passed in `args`:

| Option | Default | Description |
| --- | --- | --- |
| `--address ADDRESS` | First scale found | Bluetooth address of the scale (a UUID on macOS). Use it when several scales are nearby. |
| `--timeout SECONDS` | `10` | How long to wait when searching for, connecting to or reading from the scale. |

The server looks for a scale advertising a name that starts with `BOOKOO_SC_U` and connects on the first tool call.
If the connection drops, for example because the scale turned itself off, the next tool call reconnects once the
scale is back on.

To try the server without an AI client, open it in the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector .venv/bin/bookoo-themis-ultra-mcp
```

## Tools

Data the scale sends:

| Tool | Protocol entry | Returns |
| --- | --- | --- |
| `get_scale_data` | Receiving weight (`03 0B`) | Weight (g), flow rate (g/s), timer (ms), battery (%), display unit, auto-off time (min), beep level and flow smoothing |
| `get_powder_weight` | Receiving powder weight (`03 0F`) | The last powder (dose) weight the scale reported |
| `get_auto_mode_event` | Receiving automatic mode event and settlement data (`03 0D`) | The last event (`ready`, `started`, `stopped`, `exit_ready`, `exit_done`) with its timer, weight and result: average flow rate (timing mode) or liquid/powder ratio (ratio mode) |

Commands (`03 0A`):

| Tool | Code | Notes |
| --- | --- | --- |
| `tare` | `01` | Not while charging, or while automatic mode is running or done |
| `set_beep_level(level)` | `02` | 0 = mute, 1 = low, 2 = medium, 3 = high |
| `set_auto_off_time(minutes)` | `03` | 5–30 minutes |
| `start_timer` | `04` | Timing and ratio modes |
| `stop_timer` | `05` | Timing and ratio modes |
| `reset_timer` | `06` | Timing and ratio modes |
| `tare_and_start_timer` | `07` | In automatic mode, starts extraction when the scale is ready |
| `set_flow_smoothing(enabled)` | `08` | |
| `calibrate` | `09` | Destructive: a wrong calibration skews every later reading |
| `set_auto_stop_condition(mode, condition)` | `0B` | Firmware V4.0.0+. Mode `timing` or `ratio`; condition `flow_stopped`, `filter_removed` or `server_removed` |
| `set_auto_stop_condition_legacy(condition)` | `0B` | Firmware V3.1.2 and earlier. Condition `flow_stopped` or `container_removed` |
| `set_powder_weight(grams)` | `0D` | 0.1–999.9 g. Firmware V3.2.4 beta or V4.0.0+ |
| `shut_down` | `15` | Destructive: the scale must then be switched on by hand. Firmware V4.0.0+, not while charging |
| `reset_auto_off_countdown` | `25` | Keeps the scale on; send it periodically, well before it would turn off. Firmware V4.0.1+ |

Good to know:

- The scale does not acknowledge commands. Call `get_scale_data` afterwards to check the effect, for example that the
  weight is zero after `tare` or that the beep level changed. Changes show up within a fraction of a second.
- The firmware version can't be read over Bluetooth, so the server can't check firmware requirements. The BOOKOO app
  shows the version.
- `get_powder_weight` and `get_auto_mode_event` return the last value the scale sent since the server connected.
- Tool annotations mark the `get_*` tools as read-only and `calibrate` and `shut_down` as destructive, so clients can
  ask before running them.
- Tested on a Themis Ultra from macOS: `get_scale_data`, `set_beep_level`, `set_flow_smoothing`, `set_auto_off_time`,
  `tare`, `shut_down`, and reconnecting after the scale was switched off and on. The other tools follow the protocol
  document but haven't been tried on a scale yet; the scale sent no powder-weight or automatic-mode packets during
  testing.

## Python library

The protocol and Bluetooth layers also work without MCP:

```python
import asyncio

from bookoo_themis_ultra_mcp import protocol
from bookoo_themis_ultra_mcp.scale import BookooScale


async def main() -> None:
    scale = BookooScale()
    try:
        print((await scale.read()).weight_g)
        await scale.send(protocol.tare())
    finally:
        await scale.disconnect()


asyncio.run(main())
```

- [`protocol`](src/bookoo_themis_ultra_mcp/protocol.py): builds commands and decodes data packets. No Bluetooth.
- [`scale`](src/bookoo_themis_ultra_mcp/scale.py): `BookooScale` finds, connects to and reconnects to the scale.
- [`server`](src/bookoo_themis_ultra_mcp/server.py): the MCP tools.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e . --group dev   # needs pip 25.1 or later
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests don't need a scale.

## License

[MIT](LICENSE). This project is not affiliated with or endorsed by BOOKOO.
