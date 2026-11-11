"""Bluetooth LE connection to a BOOKOO Themis Ultra scale, built on bleak."""

import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from . import protocol
from .protocol import AutoModeEvent, PowderWeight, ScaleData

NAME_PREFIX = "BOOKOO_SC_U"
"""Advertised name prefix of the Themis Ultra, for example ``BOOKOO_SC_U 380670``."""

log = logging.getLogger(__name__)


class ScaleError(Exception):
    """The scale could not be reached or did not respond."""


class BookooScale:
    """A BOOKOO Themis Ultra scale that connects on first use and reconnects when needed.

    Args:
        address: Bluetooth address of the scale (a UUID on macOS). By default the
            first scale advertising a name that starts with ``NAME_PREFIX`` is used.
        timeout: Seconds to wait when searching for, connecting to or reading from
            the scale.

    Attributes:
        powder_weight: Last powder weight the scale reported, if any.
        auto_mode_event: Last automatic-mode event the scale reported, if any.
    """

    def __init__(self, address: str | None = None, timeout: float = 10.0) -> None:
        self.address = address
        self.timeout = timeout
        self.powder_weight: PowderWeight | None = None
        self.auto_mode_event: AutoModeEvent | None = None
        self._client: BleakClient | None = None
        self._scale_data: ScaleData | None = None
        self._scale_data_received = asyncio.Event()
        self._connecting = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        """Find the scale, connect and subscribe to its data. Does nothing if connected.

        Raises:
            ScaleError: If the scale is not found or the connection fails.
        """
        await self._connected_client()

    async def disconnect(self) -> None:
        """Disconnect from the scale, if connected."""
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    async def read(self) -> ScaleData:
        """Wait for the next live data packet and return it.

        Raises:
            ScaleError: If the scale cannot be reached or sends no data in time.
        """
        await self._connected_client()
        self._scale_data_received.clear()
        try:
            await asyncio.wait_for(self._scale_data_received.wait(), self.timeout)
        except asyncio.TimeoutError:
            raise ScaleError(f"The scale sent no data for {self.timeout:g} s. It may be off or out of range.") from None
        assert self._scale_data is not None
        return self._scale_data

    async def send(self, command: bytes) -> None:
        """Write a command built with :mod:`.protocol` to the scale.

        Raises:
            ScaleError: If the scale cannot be reached or the write fails.
        """
        client = await self._connected_client()
        try:
            await client.write_gatt_char(protocol.COMMAND_CHAR_UUID, command, response=True)
        except BleakError as exc:
            raise ScaleError(f"Could not send the command to the scale: {exc}") from exc

    async def _connected_client(self) -> BleakClient:
        async with self._connecting:
            if self._client is not None and self._client.is_connected:
                return self._client
            self._client = None
            device = await self._find()
            client = BleakClient(device, services=[protocol.SERVICE_UUID], timeout=self.timeout)
            try:
                await client.connect()
                # The protocol does not say which characteristic carries each data
                # packet, so listen to both.
                for uuid in (protocol.WEIGHT_CHAR_UUID, protocol.COMMAND_CHAR_UUID):
                    await client.start_notify(uuid, self._on_notification)
            except (BleakError, asyncio.TimeoutError) as exc:
                await client.disconnect()
                detail = str(exc) or type(exc).__name__
                raise ScaleError(f"Could not connect to the scale: {detail}") from exc
            self.powder_weight = self.auto_mode_event = None
            self._client = client
            log.info("Connected to %s (%s)", device.name, device.address)
            return client

    async def _find(self) -> BLEDevice:
        try:
            device = await BleakScanner.find_device_by_filter(self._is_scale, self.timeout)
        except BleakError as exc:
            raise ScaleError(f"Bluetooth is not available: {exc}") from exc
        if device is None:
            raise ScaleError(
                "BOOKOO Themis Ultra not found. Make sure it is on, nearby and not "
                "connected to another app, such as the BOOKOO app."
            )
        return device

    def _is_scale(self, device: BLEDevice, advertisement: AdvertisementData) -> bool:
        if self.address:
            return device.address.lower() == self.address.lower()
        return (advertisement.local_name or device.name or "").startswith(NAME_PREFIX)

    def _on_notification(self, _: object, data: bytearray) -> None:
        try:
            packet = protocol.parse_packet(bytes(data))
        except ValueError as exc:
            log.warning("Ignoring packet: %s", exc)
            return
        if isinstance(packet, ScaleData):
            self._scale_data = packet
            self._scale_data_received.set()
        elif isinstance(packet, PowderWeight):
            self.powder_weight = packet
        elif isinstance(packet, AutoModeEvent):
            self.auto_mode_event = packet
