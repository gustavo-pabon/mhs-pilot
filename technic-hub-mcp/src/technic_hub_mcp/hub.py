"""Bluetooth LE connection to a LEGO Technic Hub, built on bleak.

TechnicHub connects on first use and reconnects when needed. It matches the hub's replies to requests, follows the
feedback the hub gives about motor commands, and gathers what the MCP tools return.
"""

import asyncio
import contextlib
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from . import protocol
from .models import (
    HubAdvertisement,
    HubAlerts,
    HubInfo,
    HubSensors,
    ModeInfo,
    MotorState,
    MotorTravel,
    MoveResult,
    Port,
    PortInfo,
    PortList,
    PortReading,
    ValueFormat,
    ValueRange,
)
from .protocol import (
    AlertType,
    AttachedIO,
    EndState,
    Feedback,
    GenericError,
    HubAction,
    HubActionUpdate,
    HubAlertUpdate,
    HubProperty,
    HubPropertyUpdate,
    IOEvent,
    MessageType,
    PortInformationType,
    PortInputFormat,
)
from .protocol import ModeInformationType as Info

log = logging.getLogger(__name__)

REPLY_TIMEOUT = 2.0
"""Seconds to wait for the answer to a request; the hub normally answers within 0.1 s."""
OPTIONAL_REPLY_TIMEOUT = 0.5
"""Seconds to wait for information a device may not provide."""
COMMAND_TIMEOUT = 0.5
"""Seconds to wait for the hub to acknowledge a motor command before sending it again; it normally takes 0.05 s.

After a move that ends holding its position, the hub sometimes ignores the next command without any reply."""
ERROR_WINDOW = 0.3
"""Seconds to wait for an error after a message the hub doesn't answer."""
PORTS_TIMEOUT = 2.0
"""Seconds to wait for the hub to list its ports after connecting."""
POLL_INTERVAL = 0.2
"""Seconds between position checks while a move runs."""
STALL_TIME = 1.0
STALL_DEGREES = 2
"""A moving motor that turns less than STALL_DEGREES in STALL_TIME seconds counts as stalled and is braked."""
SETTLE_TIME = 0.2
"""Seconds to let a motor come to rest before reading where it stopped."""
STOPPED_TIME = 0.4
"""Seconds a move must have met its goal (run time up, targets reached) before it counts as completed."""
REACHED_DEGREES = 5
"""How close to its target, in degrees, a motor must get for the target to count as reached."""
TIMED_MOVE_MARGIN = 2.0
"""Seconds past its run time after which a timed run that still hasn't finished is braked."""
UNTRACKED_MOVE_TIMEOUT = 60.0
"""Seconds to wait for a move whose progress can't be followed, such as one without a position sensor."""
INTERNAL_PORTS = range(50, 101)

Outcome = str  # "completed" or "interrupted"


class HubError(Exception):
    """The hub could not be reached, did not answer, or rejected a request."""


class NoReplyError(HubError):
    """The hub did not answer a request in time."""


class RejectedError(HubError):
    """The hub answered a request with an error."""


@dataclass(eq=False)
class _Waiter:
    command: int
    matches: Callable[[protocol.Message], bool]
    future: "asyncio.Future[protocol.Message]"
    accepts_ack: bool = False


@dataclass(eq=False)
class _Command:
    """A port output command and the feedback the hub gives about it."""

    accepted: "asyncio.Future[int]"
    done: "asyncio.Future[Outcome]"


@dataclass
class _OutputState:
    """The command of a port waiting for the hub to acknowledge it, and the one it is running."""

    pending: _Command | None = None
    running: _Command | None = None


class TechnicHub:
    """A LEGO Technic Hub that connects on first use and reconnects when needed.

    Args:
        address: Bluetooth address of the hub (a UUID on macOS). By default the first Technic Hub found is used.
        timeout: Seconds to wait when searching for or connecting to the hub.

    Attributes:
        ports: The devices attached to the hub, by port ID, as the hub last reported them.
    """

    def __init__(self, address: str | None = None, timeout: float = 10.0) -> None:
        self.address = address
        self.timeout = timeout
        self.ports: dict[int, AttachedIO] = {}
        self._client: BleakClient | None = None
        self._device: BLEDevice | None = None
        self._advertisement: AdvertisementData | None = None
        self._connecting = asyncio.Lock()
        self._exchanging = asyncio.Lock()
        self._waiters: list[_Waiter] = []
        self._outputs: dict[int, _OutputState] = {}
        self._input_modes: dict[int, int] = {}
        self._port_information: dict[int, protocol.PortInformation] = {}
        self._mode_payloads: dict[tuple[int, int, int], bytes | None] = {}
        self._ports_changed_at = 0.0

    # Connection

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        """Find the hub and connect to it. Does nothing if connected.

        Raises:
            HubError: If the hub is not found or the connection fails.
        """
        await self._connected_client()

    async def disconnect(self) -> None:
        """Brake the motors and disconnect from the hub, if connected."""
        client, self._client = self._client, None
        self._fail_all(HubError("Disconnected from the hub."))
        if client is None or not client.is_connected:
            return
        for port, device in list(self.ports.items()):
            if device.event == IOEvent.ATTACHED and device.io_type in protocol.MOTORS:
                with contextlib.suppress(BleakError):
                    command = protocol.start_power(port, EndState.BRAKE)
                    await client.write_gatt_char(protocol.CHARACTERISTIC_UUID, command, response=True)
        await client.disconnect()

    async def _connected_client(self) -> BleakClient:
        async with self._connecting:
            if self._client is not None and self._client.is_connected:
                return self._client
            self._client = None
            self._forget_ports()
            device, advertisement = await self._find()
            client = BleakClient(device, self._on_disconnect, services=[protocol.SERVICE_UUID], timeout=self.timeout)
            try:
                await client.connect()
                self._client = client
                await client.start_notify(protocol.CHARACTERISTIC_UUID, self._on_notification)
            except (BleakError, asyncio.TimeoutError) as exc:
                self._client = None
                await client.disconnect()
                detail = str(exc) or type(exc).__name__
                raise HubError(f"Could not connect to the Technic Hub: {detail}") from exc
            self._device, self._advertisement = device, advertisement
            await self._wait_for_ports()
            log.info("Connected to %s (%s)", device.name, device.address)
            return client

    async def _find(self) -> tuple[BLEDevice, AdvertisementData]:
        found: list[AdvertisementData] = []

        def is_hub(device: BLEDevice, advertisement: AdvertisementData) -> bool:
            if self._is_hub(device, advertisement):
                found.append(advertisement)
                return True
            return False

        try:
            device = await BleakScanner.find_device_by_filter(is_hub, self.timeout)
        except BleakError as exc:
            raise HubError(f"Bluetooth is not available: {exc}") from exc
        if device is None:
            raise HubError(
                "LEGO Technic Hub not found. Press the hub's button so that its light flashes, and make sure it "
                "is not connected to another app or device."
            )
        return device, found[-1]

    def _is_hub(self, device: BLEDevice, advertisement: AdvertisementData) -> bool:
        if self.address:
            return device.address.lower() == self.address.lower()
        lego = advertisement.manufacturer_data.get(protocol.LEGO_COMPANY_ID, b"")
        return protocol.SERVICE_UUID in advertisement.service_uuids and lego[1:2] == bytes([protocol.TECHNIC_HUB])

    async def _wait_for_ports(self) -> None:
        """Wait until the hub has listed the devices on its ports, which it does once notifications are on."""
        deadline = time.monotonic() + PORTS_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.ports and time.monotonic() - self._ports_changed_at > 0.2:
                return

    def _on_disconnect(self, client: BleakClient) -> None:
        if client is self._client:
            log.info("The hub disconnected")
            self._fail_all(HubError("The hub disconnected. It may have been switched off or gone out of range."))

    def _fail_all(self, error: HubError) -> None:
        for waiter in self._waiters:
            _fail(waiter.future, error)
        for state in self._outputs.values():
            for command in (state.pending, state.running):
                if command is not None:
                    _fail(command.accepted, error)
                    _fail(command.done, error)
        self._outputs.clear()

    def _forget_ports(self) -> None:
        self.ports.clear()
        self._outputs.clear()
        self._input_modes.clear()
        self._port_information.clear()
        self._mode_payloads.clear()

    def _forget_port(self, port: int) -> None:
        self._input_modes.pop(port, None)
        self._port_information.pop(port, None)
        for key in [key for key in self._mode_payloads if key[0] == port]:
            del self._mode_payloads[key]

    # Messages from the hub

    def _on_notification(self, _: object, data: bytearray) -> None:
        try:
            frames = protocol.split_messages(bytes(data))
        except ValueError as exc:
            log.warning("Ignoring notification: %s", exc)
            return
        for frame in frames:
            try:
                message = protocol.parse_message(frame)
            except ValueError as exc:
                log.warning("Ignoring message: %s", exc)
                continue
            if message is not None:
                self._dispatch(message)

    def _dispatch(self, message: protocol.Message) -> None:
        if isinstance(message, protocol.OutputFeedback):
            for port, flags in message.feedback:
                self._on_feedback(port, flags)
            return
        if isinstance(message, GenericError):
            self._on_error(message)
            return
        if isinstance(message, AttachedIO):
            self._ports_changed_at = time.monotonic()
            self._forget_port(message.port)
            if message.event == IOEvent.DETACHED:
                self.ports.pop(message.port, None)
            else:
                self.ports[message.port] = message
        elif isinstance(message, PortInputFormat):
            self._input_modes[message.port] = message.mode
        elif isinstance(message, HubActionUpdate):
            log.info("The hub reported action 0x%02x", message.action)
        for waiter in self._waiters:
            if not waiter.future.done() and waiter.matches(message):
                waiter.future.set_result(message)
                return

    def _on_feedback(self, port: int, flags: int) -> None:
        """Follow a port's commands through the hub's feedback, per the protocol's buffering state machine.

        Commands always run immediately, so a port has at most one running command and one waiting for the hub
        to acknowledge it, and feedback about the running command comes first.
        """
        state = self._outputs.get(port)
        if state is None:
            return
        if flags & Feedback.DISCARDED and state.running is not None:
            _settle(state.running.done, "interrupted")
            state.running = None
        if state.running is not None and flags & Feedback.COMPLETED:
            _settle(state.running.done, "completed")
            state.running = None
            return
        pending = state.pending
        if pending is None or pending.accepted.done():
            return
        pending.accepted.set_result(flags)
        if flags & Feedback.COMPLETED:
            _settle(pending.done, "completed")
        elif flags & Feedback.IDLE:
            _settle(pending.done, "interrupted")
        else:
            state.running = pending

    def _on_error(self, error: GenericError) -> None:
        if error.code in (0x01, 0x02):  # ACK and MACK only inform
            for waiter in self._waiters:
                if waiter.accepts_ack and waiter.command == error.command and not waiter.future.done():
                    waiter.future.set_result(error)
                    return
            return
        reason = protocol.ERROR_CODES.get(error.code, f"error 0x{error.code:02x}")
        exc = RejectedError(f"The hub rejected the request: {reason}.")
        if error.command == MessageType.PORT_OUTPUT_COMMAND:
            for state in self._outputs.values():
                if state.pending is not None and not state.pending.accepted.done():
                    state.pending.accepted.set_exception(exc)
                    return
        for waiter in self._waiters:
            if waiter.command == error.command and not waiter.future.done():
                waiter.future.set_exception(exc)
                return
        log.warning("The hub reported %s for message type 0x%02x", reason, error.command)

    # Requests

    async def _request(
        self,
        message: bytes,
        matches: Callable[[protocol.Message], bool],
        timeout: float = REPLY_TIMEOUT,
        accepts_ack: bool = False,
    ) -> protocol.Message:
        """Send a message and return the hub's answer: the first message for which ``matches`` is true."""
        async with self._exchanging:
            client = await self._connected_client()
            waiter = _Waiter(message[2], matches, _new_future(), accepts_ack)
            self._waiters.append(waiter)
            try:
                await self._write(client, message)
                return await asyncio.wait_for(waiter.future, timeout)
            except asyncio.TimeoutError:
                raise NoReplyError(f"The hub did not answer within {timeout:g} s.") from None
            finally:
                self._waiters.remove(waiter)

    async def _send(self, message: bytes) -> None:
        """Send a message the hub doesn't answer, waiting briefly in case it reports an error."""
        with contextlib.suppress(NoReplyError):
            await self._request(message, lambda _: False, ERROR_WINDOW, accepts_ack=True)

    async def _write(self, client: BleakClient, message: bytes) -> None:
        try:
            await client.write_gatt_char(protocol.CHARACTERISTIC_UUID, message, response=True)
        except BleakError as exc:
            raise HubError(f"Could not send to the hub: {exc}") from exc

    async def _output(self, command: bytes) -> _Command:
        """Send a port output command; return it once the hub acknowledges it, sending it twice if ignored."""
        port = command[3]
        async with self._exchanging:
            client = await self._connected_client()
            state = self._outputs.setdefault(port, _OutputState())
            pending = state.pending = _Command(_new_future(), _new_future())
            if command[5] == protocol.WRITE_DIRECT_MODE_DATA:
                self._input_modes.pop(port, None)  # the device may now be in another mode
            try:
                for _ in range(2):
                    await self._write(client, command)
                    try:
                        await asyncio.wait_for(asyncio.shield(pending.accepted), COMMAND_TIMEOUT)
                        return pending
                    except asyncio.TimeoutError:
                        log.info("The hub ignored a command for port %s", self.port_name(port))
                raise NoReplyError(f"The hub ignored the command for port {self.port_name(port)} twice.")
            finally:
                if state.pending is pending:
                    state.pending = None

    # Hub

    async def hub_info(self) -> HubInfo:
        """Read all of the hub's properties."""
        await self._connected_client()
        values: dict[int, str | int | bool] = {}
        for prop in HubProperty:
            try:
                values[prop] = protocol.decode_hub_property(prop, await self._read_property(prop))
            except (NoReplyError, RejectedError, ValueError) as exc:
                log.info("Hub property 0x%02x unavailable: %s", prop, exc)
        system_type = values.get(HubProperty.SYSTEM_TYPE_ID)
        assert self._device is not None
        return HubInfo(
            name=values.get(HubProperty.ADVERTISING_NAME),
            address=self._device.address,
            button_pressed=values.get(HubProperty.BUTTON),
            firmware_version=values.get(HubProperty.FW_VERSION),
            hardware_version=values.get(HubProperty.HW_VERSION),
            rssi=values.get(HubProperty.RSSI),
            battery_percent=values.get(HubProperty.BATTERY_VOLTAGE),
            battery_type=values.get(HubProperty.BATTERY_TYPE),
            manufacturer=values.get(HubProperty.MANUFACTURER_NAME),
            radio_firmware_version=values.get(HubProperty.RADIO_FIRMWARE_VERSION),
            lwp_version=values.get(HubProperty.LWP_PROTOCOL_VERSION),
            system_type_id=system_type,
            system_type=protocol.SYSTEM_TYPES.get(system_type) if isinstance(system_type, int) else None,
            hw_network_id=values.get(HubProperty.HW_NETWORK_ID),
            hw_network_family=values.get(HubProperty.HW_NETWORK_FAMILY),
            primary_mac_address=values.get(HubProperty.PRIMARY_MAC_ADDRESS),
            secondary_mac_address=values.get(HubProperty.SECONDARY_MAC_ADDRESS),
            advertisement=self._advertised(),
        )

    async def hub_alerts(self) -> HubAlerts:
        """Read the state of the hub's four alerts."""
        await self._connected_client()
        states: dict[int, bool | None] = {}
        for alert in AlertType:
            try:
                reply = await self._request(protocol.request_hub_alert(alert), _alert_update(alert), REPLY_TIMEOUT)
                states[alert] = reply.active
            except (NoReplyError, RejectedError) as exc:
                log.info("Hub alert 0x%02x unavailable: %s", alert, exc)
                states[alert] = None
        return HubAlerts(
            low_voltage=states[AlertType.LOW_VOLTAGE],
            high_current=states[AlertType.HIGH_CURRENT],
            low_signal_strength=states[AlertType.LOW_SIGNAL_STRENGTH],
            over_power_condition=states[AlertType.OVER_POWER_CONDITION],
        )

    async def set_name(self, name: str | None) -> str:
        """Rename the hub, or restore its default name if ``name`` is None; return the name it now reports."""
        await self._send(protocol.set_advertising_name(name) if name else protocol.reset_advertising_name())
        return protocol.decode_text(await self._read_property(HubProperty.ADVERTISING_NAME))

    async def hub_action(self, action: HubAction) -> None:
        """Ask the hub to switch off, disconnect, switch its port power, or show or clear its busy light."""
        if action not in (HubAction.SWITCH_OFF, HubAction.DISCONNECT):
            await self._send(protocol.hub_action(action))
            return
        announced = HubAction.WILL_SWITCH_OFF if action == HubAction.SWITCH_OFF else HubAction.WILL_DISCONNECT
        try:
            await self._request(protocol.hub_action(action), lambda m: _is_action(m, announced))
        except HubError:
            if self.is_connected:  # the hub can go before it says so
                raise

    async def _read_property(self, prop: int) -> bytes:
        reply = await self._request(protocol.request_hub_property(prop), _property_update(prop))
        return reply.payload

    def _advertised(self) -> HubAdvertisement | None:
        if self._advertisement is None:
            return None
        try:
            data = protocol.parse_advertisement(self._advertisement.manufacturer_data[protocol.LEGO_COMPANY_ID])
        except (KeyError, ValueError):
            return None
        return HubAdvertisement(
            signal_dbm=self._advertisement.rssi,
            button_pressed=data.button_pressed,
            system_type_id=data.system_type,
            capabilities=protocol.decode_flags(data.capabilities, protocol.HUB_CAPABILITIES),
            last_network_id=data.last_network,
            status=protocol.decode_flags(data.status, protocol.HUB_STATUS),
        )

    # Ports

    async def port_id(self, port: str | int) -> int:
        """Return the ID of an attached port, given as a letter, a pair of letters or a number.

        Raises:
            HubError: If nothing is attached to that port, or the hub can't be reached.
        """
        await self._connected_client()
        key = port.strip().upper() if isinstance(port, str) else port
        if isinstance(key, str) and key.isdigit():
            key = int(key)
        for port_id in self.ports:
            if key in (port_id, self.port_name(port_id)):
                return port_id
        attached = ", ".join(self.port_name(port_id) for port_id in sorted(self.ports))
        hint = " Pair two ports with connect_virtual_port first." if isinstance(key, str) and len(key) == 2 else ""
        raise HubError(f"Nothing is attached to port {port!r}.{hint} Attached ports: {attached}.")

    def port_name(self, port: int) -> str:
        """Return the name tools use for a port: A-D for connectors, AB for a virtual pair, else its number."""
        device = self.ports.get(port)
        if device is not None and device.event == IOEvent.ATTACHED_VIRTUAL:
            assert device.port_a is not None and device.port_b is not None
            return self.port_name(device.port_a) + self.port_name(device.port_b)
        return chr(ord("A") + port) if port < 26 else str(port)

    def is_virtual(self, port: int) -> bool:
        device = self.ports.get(port)
        return device is not None and device.event == IOEvent.ATTACHED_VIRTUAL

    async def list_ports(self) -> PortList:
        """List the devices on the hub's ports, as the hub reported them."""
        await self._connected_client()
        return PortList(ports=[self._port(port) for port in sorted(self.ports)])

    async def find_port(self, io_type: int) -> int:
        """Return the first port with a device of type ``io_type``, such as the hub's own light."""
        await self._connected_client()
        for port, device in sorted(self.ports.items()):
            if device.event == IOEvent.ATTACHED and device.io_type == io_type:
                return port
        raise HubError(f"The hub has no {protocol.IO_TYPES.get(io_type, f'device of type {io_type}')}.")

    async def port_info(self, port: int) -> PortInfo:
        """Describe a port's device and each of its modes."""
        information = await self._port_information_of(port)
        combinations = None
        if information.capabilities & 0x04:  # logically combinable
            with contextlib.suppress(NoReplyError, RejectedError):
                reply = await self._request(
                    protocol.request_port_information(port, PortInformationType.POSSIBLE_MODE_COMBINATIONS),
                    lambda m: isinstance(m, protocol.PortModeCombinations) and m.port == port,
                )
                combinations = protocol.decode_mode_combinations(reply.combinations)
        device = self.ports[port]
        return PortInfo(
            port=self.port_name(port),
            port_id=port,
            device_type_id=device.io_type or 0,
            device=protocol.IO_TYPES.get(device.io_type or 0),
            capabilities=protocol.decode_flags(information.capabilities, protocol.PORT_CAPABILITIES),
            modes=[await self._mode(port, mode, information) for mode in range(information.mode_count)],
            mode_combinations=combinations,
        )

    async def mode_number(self, port: int, mode: int | str) -> int:
        """Return a mode number given its number or its name, such as "POS"."""
        if isinstance(mode, int) or mode.strip().isdigit():
            return int(mode)
        names = await self._mode_names(port)
        for number, name in names.items():
            if name.upper() == mode.strip().upper():
                return number
        raise HubError(f"Port {self.port_name(port)} has no mode {mode!r}. Its modes: {', '.join(names.values())}.")

    async def find_mode(self, port: int, *names: str) -> int | None:
        """Return the number of the first mode whose name is one of ``names``, or None."""
        modes = {name.upper(): number for number, name in (await self._mode_names(port)).items()}
        return next((modes[name] for name in names if name in modes), None)

    async def read_value(self, port: int, mode: int) -> PortReading:
        """Put a port's device in ``mode`` and read its values, scaled to SI units with the mode's ranges."""
        information = await self._port_information_of(port)
        if not information.input_modes >> mode & 1:
            inputs = [str(m) for m in range(16) if information.input_modes >> m & 1]
            raise HubError(
                f"Mode {mode} of port {self.port_name(port)} doesn't report values. "
                f"Modes that do: {', '.join(inputs) or 'none'}."
            )
        raw = await self._read_raw(port, mode)
        value_format = await self._value_format(port, mode)
        raw_range = await self._range(port, mode, Info.RAW)
        si_range = await self._range(port, mode, Info.SI)
        device = self.ports[port]
        return PortReading(
            port=self.port_name(port),
            device=protocol.IO_TYPES.get(device.io_type or 0),
            mode=mode,
            mode_name=await self._text(port, mode, Info.NAME),
            values=[_scale(value, raw_range, si_range, value_format) for value in raw],
            unit=await self._text(port, mode, Info.SYMBOL),
            raw_values=raw,
        )

    async def hub_sensors(self) -> HubSensors:
        """Read each of the hub's internal sensors in its first input mode."""
        await self._connected_client()
        readings = []
        for port, device in sorted(self.ports.items()):
            if device.event != IOEvent.ATTACHED or port not in INTERNAL_PORTS:
                continue
            try:
                inputs = (await self._port_information_of(port)).input_modes
                if inputs:
                    readings.append(await self.read_value(port, (inputs & -inputs).bit_length() - 1))
            except (NoReplyError, RejectedError) as exc:
                log.warning("Could not read port %d: %s", port, exc)
        return HubSensors(readings=readings)

    async def motor_state(self, port: int) -> MotorState:
        """Read a motor's speed, encoder position, absolute position and load, where it reports them."""
        modes = {name.upper(): number for number, name in (await self._mode_names(port)).items()}
        if not {"SPEED", "POS", "APOS"} & modes.keys():
            raise HubError(f"Port {self.port_name(port)} has no motor with a rotation sensor.")

        async def read(name: str) -> int | None:
            return int((await self._read_raw(port, modes[name]))[0]) if name in modes else None

        return MotorState(
            port=self.port_name(port),
            speed_percent=await read("SPEED"),
            position_degrees=await read("POS"),
            absolute_position_degrees=await read("APOS"),
            load=await read("LOAD"),
        )

    async def connect_virtual_port(self, port_a: int, port_b: int) -> Port:
        """Pair two ports into a virtual port, or return the existing pair."""
        pair = {port_a, port_b}
        for port, device in self.ports.items():
            if device.event == IOEvent.ATTACHED_VIRTUAL and {device.port_a, device.port_b} == pair:
                return self._port(port)

        def is_pair(m: protocol.Message) -> bool:
            return isinstance(m, AttachedIO) and m.event == IOEvent.ATTACHED_VIRTUAL and {m.port_a, m.port_b} == pair

        reply = await self._request(protocol.connect_virtual_port(port_a, port_b), is_pair)
        return self._port(reply.port)

    async def disconnect_virtual_port(self, port: int) -> None:
        """Remove a virtual port."""
        if not self.is_virtual(port):
            raise HubError(f"Port {self.port_name(port)} is not a virtual port.")
        await self._request(
            protocol.disconnect_virtual_port(port),
            lambda m: isinstance(m, AttachedIO) and m.port == port and m.event == IOEvent.DETACHED,
        )

    def _port(self, port: int) -> Port:
        device = self.ports[port]
        virtual = device.event == IOEvent.ATTACHED_VIRTUAL
        return Port(
            port=self.port_name(port),
            port_id=port,
            kind="virtual" if virtual else "connector" if port < INTERNAL_PORTS.start else "internal",
            device_type_id=device.io_type or 0,
            device=protocol.IO_TYPES.get(device.io_type or 0),
            hardware_revision=device.hardware_revision,
            software_revision=device.software_revision,
            pair=[self.port_name(device.port_a), self.port_name(device.port_b)]
            if virtual and device.port_a is not None and device.port_b is not None
            else None,
        )

    async def _port_information_of(self, port: int) -> protocol.PortInformation:
        if port not in self._port_information:
            reply = await self._request(
                protocol.request_port_information(port, PortInformationType.MODE_INFO),
                lambda m: isinstance(m, protocol.PortInformation) and m.port == port,
            )
            self._port_information[port] = reply
        return self._port_information[port]

    async def _mode_information(self, port: int, mode: int, info_type: int, optional: bool = True) -> bytes | None:
        """Return one Port Mode Information payload; None if the device doesn't provide it and it's optional."""
        key = (port, mode, info_type)
        if key not in self._mode_payloads:
            try:
                reply = await self._request(
                    protocol.request_port_mode_information(port, mode, info_type),
                    lambda m: isinstance(m, protocol.PortModeInformation) and (m.port, m.mode, m.info_type) == key,
                    OPTIONAL_REPLY_TIMEOUT if optional else REPLY_TIMEOUT,
                )
                self._mode_payloads[key] = reply.payload
            except (NoReplyError, RejectedError):
                if not optional:
                    raise
                self._mode_payloads[key] = None
        return self._mode_payloads[key]

    async def _mode_names(self, port: int) -> dict[int, str]:
        information = await self._port_information_of(port)
        names = {}
        for mode in range(information.mode_count):
            name = await self._text(port, mode, Info.NAME)
            if name:
                names[mode] = name
        return names

    async def _text(self, port: int, mode: int, info_type: int) -> str | None:
        payload = await self._mode_information(port, mode, info_type)
        return protocol.decode_text(payload) if payload is not None else None

    async def _range(self, port: int, mode: int, info_type: int) -> tuple[float, float] | None:
        payload = await self._mode_information(port, mode, info_type)
        try:
            return protocol.decode_range(payload) if payload is not None else None
        except ValueError:
            return None

    async def _value_format(self, port: int, mode: int) -> protocol.ValueFormat:
        payload = await self._mode_information(port, mode, Info.VALUE_FORMAT, optional=False)
        assert payload is not None
        try:
            return protocol.decode_value_format(payload)
        except ValueError as exc:
            raise HubError(f"Port {self.port_name(port)} sent an unreadable value format: {exc}") from exc

    async def _mode(self, port: int, mode: int, information: protocol.PortInformation) -> ModeInfo:
        mapping = await self._mode_information(port, mode, Info.MAPPING)
        bias = await self._mode_information(port, mode, Info.MOTOR_BIAS)
        capabilities = await self._mode_information(port, mode, Info.CAPABILITY_BITS)
        value_format = None
        with contextlib.suppress(HubError):
            raw_format = await self._value_format(port, mode)
            value_format = ValueFormat(
                datasets=raw_format.datasets,
                dataset_type=protocol.DATASET_TYPES[raw_format.dataset_type][0],
                figures=raw_format.figures,
                decimals=raw_format.decimals,
            )
        return ModeInfo(
            mode=mode,
            name=await self._text(port, mode, Info.NAME),
            input=bool(information.input_modes >> mode & 1),
            output=bool(information.output_modes >> mode & 1),
            raw_range=_value_range(await self._range(port, mode, Info.RAW)),
            percent_range=_value_range(await self._range(port, mode, Info.PCT)),
            si_range=_value_range(await self._range(port, mode, Info.SI)),
            symbol=await self._text(port, mode, Info.SYMBOL),
            input_mapping=protocol.decode_flags(mapping[0], protocol.MAPPING_FLAGS) if mapping else None,
            output_mapping=protocol.decode_flags(mapping[1], protocol.MAPPING_FLAGS)
            if mapping and len(mapping) > 1
            else None,
            motor_bias_percent=bias[0] if bias else None,
            capability_bits=capabilities[:6].hex() if capabilities else None,
            value_format=value_format,
        )

    async def _select_mode(self, port: int, mode: int) -> None:
        if self._input_modes.get(port) != mode:
            await self._request(
                protocol.set_port_input_format(port, mode),
                lambda m: isinstance(m, PortInputFormat) and m.port == port and m.mode == mode,
            )

    async def _read_raw(self, port: int, mode: int) -> list[int | float]:
        """Put a port's device in ``mode`` and return its values as sent."""
        value_format = await self._value_format(port, mode)
        for attempt in range(2):
            await self._select_mode(port, mode)
            reply = await self._request(
                protocol.request_port_information(port, PortInformationType.PORT_VALUE),
                lambda m: isinstance(m, protocol.PortValue) and m.port == port,
            )
            try:
                return protocol.decode_values(reply.data, value_format)
            except ValueError as exc:
                self._input_modes.pop(port, None)  # the device was in another mode: set it again
                if attempt:
                    raise HubError(f"Port {self.port_name(port)} sent an unexpected value: {exc}") from exc
        raise AssertionError("unreachable")

    async def read_position(self, port: int) -> int | None:
        """Return a motor's encoder position in degrees, or None if its device has no position mode."""
        mode = await self.find_mode(port, "POS")
        return None if mode is None else int((await self._read_raw(port, mode))[0])

    # Port output commands

    async def output(self, command: bytes) -> None:
        """Send a port output command built with :mod:`.protocol` and wait until the hub acknowledges it.

        Raises:
            HubError: If the hub rejects or ignores the command, or can't be reached.
        """
        await self._output(command)

    async def write_mode(self, command: bytes) -> None:
        """Send a WriteDirectModeData command after putting the device in its mode, as lights need."""
        await self._select_mode(command[3], command[6])
        await self._output(command)

    async def move(self, command: bytes, *, wait: bool = True) -> MoveResult:
        """Send a motor command and, if ``wait``, follow it until the motors stop.

        The move counts as completed when the hub reports it or, as this hub doesn't for timed and paired moves,
        once its run time is up or every motor has reached its target. A motor that stops turning before then is
        braked and reported as stalled.
        """
        port = command[3]
        motors = self._motors(port)
        start = [await self._position_or_none(motor) for motor in motors]
        started = time.monotonic()
        sent = await self._output(command)
        if not wait:
            outcome = sent.done.result() if sent.done.done() else "started"
            return self._move_result(outcome, None, motors, start, [None] * len(motors))
        outcome = await self._follow(sent, port, motors, start, _plan(command, start))
        seconds = round(time.monotonic() - started, 2)
        await asyncio.sleep(SETTLE_TIME)
        end = [await self._position_or_none(motor) for motor in motors]
        return self._move_result(outcome, seconds, motors, start, end)

    async def _follow(
        self, sent: _Command, port: int, motors: list[int], start: list[int | None], plan: "_Plan"
    ) -> str:
        track = plan.moving and None not in start
        begun = time.monotonic()
        if plan.duration is not None:
            deadline: float | None = begun + plan.duration + TIMED_MOVE_MARGIN
        else:
            deadline = None if track else begun + UNTRACKED_MOVE_TIMEOUT
        positions, moved_at, moved, reached_at, read_at = start, begun, False, None, begun
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(sent.done), POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            current = [await self._position_or_none(motor) for motor in motors] if track else []
            readable = track and None not in current
            if readable:
                read_at = now
                if any(abs(c - p) >= STALL_DEGREES for c, p in zip(current, positions)):  # type: ignore[operator]
                    positions, moved_at, moved = current, now, True
                if plan.targets is not None and _reached(start, current, plan.targets):  # type: ignore[arg-type]
                    reached_at = now if reached_at is None else reached_at
                    if now - reached_at >= STOPPED_TIME:
                        return "completed"
                else:
                    reached_at = None
            if plan.duration is not None and now - begun >= plan.duration + STOPPED_TIME and (moved or not track):
                return "completed"
            if deadline is not None and now >= deadline:
                break
            if readable and now - moved_at >= STALL_TIME:
                break
            if track and now - read_at >= UNTRACKED_MOVE_TIMEOUT:
                break
        if sent.done.done():
            return sent.done.result()
        log.info("Port %s stopped turning before the end of its command; braking it", self.port_name(port))
        try:
            await self._output(protocol.start_power(port, EndState.BRAKE))
        except (NoReplyError, RejectedError) as exc:
            log.warning("Could not brake port %s: %s", self.port_name(port), exc)
        return "stalled"

    def _motors(self, port: int) -> list[int]:
        device = self.ports.get(port)
        if device is not None and device.event == IOEvent.ATTACHED_VIRTUAL:
            assert device.port_a is not None and device.port_b is not None
            return [device.port_a, device.port_b]
        return [port]

    async def _position_or_none(self, port: int) -> int | None:
        try:
            return await self.read_position(port)
        except (NoReplyError, RejectedError) as exc:
            log.info("Could not read the position of port %s: %s", self.port_name(port), exc)
            return None

    def _move_result(
        self, outcome: str, seconds: float | None, motors: list[int], start: list[int | None], end: list[int | None]
    ) -> MoveResult:
        return MoveResult(
            outcome=outcome,
            seconds=seconds,
            motors=[
                MotorTravel(port=self.port_name(motor), start_position=s, end_position=e)
                for motor, s, e in zip(motors, start, end)
            ],
        )


def _new_future() -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(lambda f: f.cancelled() or f.exception())  # an unawaited failure is not an error
    return future


@dataclass(frozen=True)
class _Plan:
    """What a motor command should do: run for a time, or bring its motors to target positions."""

    duration: float | None = None
    targets: list[int] | None = None
    moving: bool = True


def _plan(command: bytes, start: list[int | None]) -> _Plan:
    """Work out what a motor command should do from its bytes, given where its motors start."""
    sub_command, payload = command[5], command[6:]
    if sub_command in (0x09, 0x0A):  # StartSpeedForTime, single or paired
        speeds = payload[2:3] if sub_command == 0x09 else payload[2:4]
        return _Plan(duration=struct.unpack_from("<h", payload)[0] / 1000, moving=any(speeds))
    if None in start:
        return _Plan()
    positions: list[int] = start  # type: ignore[assignment]
    if sub_command == 0x0B:  # StartSpeedForDegrees: on a pair, both motors turn that far
        degrees, speed = struct.unpack_from("<ib", payload)
        return _Plan(targets=[p + degrees * _sign(speed) for p in positions])
    if sub_command == 0x0C:  # StartSpeedForDegrees for a pair: shared by speed, per the protocol's Tacho Math
        degrees, speed_1, speed_2 = struct.unpack_from("<ibb", payload)
        total = abs(speed_1) + abs(speed_2)
        if not total:
            return _Plan()
        return _Plan(targets=[p + round(2 * degrees * s / total) for p, s in zip(positions, (speed_1, speed_2))])
    if sub_command == 0x0D and len(positions) == 1:  # GotoAbsolutePosition
        return _Plan(targets=[struct.unpack_from("<i", payload)[0]])
    if sub_command == 0x0E:  # GotoAbsolutePosition for a pair
        return _Plan(targets=list(struct.unpack_from("<ii", payload)))
    return _Plan()


def _reached(start: list[int], current: list[int], targets: list[int]) -> bool:
    """Whether every motor is within REACHED_DEGREES of its target, or past it."""
    return all((t - c) * _sign(t - s) <= REACHED_DEGREES for s, c, t in zip(start, current, targets))


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _settle(future: asyncio.Future, result: Outcome) -> None:
    if not future.done():
        future.set_result(result)


def _fail(future: asyncio.Future, error: Exception) -> None:
    if not future.done():
        future.set_exception(error)


def _property_update(prop: int) -> Callable[[protocol.Message], bool]:
    return lambda m: isinstance(m, HubPropertyUpdate) and m.property == prop


def _alert_update(alert: int) -> Callable[[protocol.Message], bool]:
    return lambda m: isinstance(m, HubAlertUpdate) and m.alert == alert


def _is_action(message: protocol.Message, action: int) -> bool:
    return isinstance(message, HubActionUpdate) and message.action == action


def _value_range(bounds: tuple[float, float] | None) -> ValueRange | None:
    return None if bounds is None else ValueRange(min=bounds[0], max=bounds[1])


def _scale(
    value: float,
    raw_range: tuple[float, float] | None,
    si_range: tuple[float, float] | None,
    value_format: protocol.ValueFormat,
) -> int | float:
    """Map a raw value linearly from the mode's raw range to its SI range, rounded to the mode's decimals."""
    result: float = value
    if raw_range and si_range and raw_range != si_range and raw_range[0] != raw_range[1]:
        (raw_min, raw_max), (si_min, si_max) = raw_range, si_range
        result = si_min + (value - raw_min) * (si_max - si_min) / (raw_max - raw_min)
    if value_format.dataset_type == 3:  # float: keep a sensible precision
        return round(result, value_format.decimals or 4)
    rounded = round(result, value_format.decimals)
    return int(rounded) if value_format.decimals == 0 else rounded
