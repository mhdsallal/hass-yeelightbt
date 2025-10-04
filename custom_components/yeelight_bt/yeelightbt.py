"""Creator : hcoohb
License : MIT
Source  : https://github.com/hcoohb/hass-yeelightbt

Hardened connection layer for Yeelight Candela/Bedside:

- Uses bleak_retry_connector.establish_connection (robust in Home Assistant).
- Serializes connect and GATT operations to avoid races/timeouts.
- Starts notifications once per session and keeps them active.
- Ensures pairing before sending commands; supports first-time button prompt.
- Writes default to response=False (Candela-friendly), with one reconnect retry.
- Safe read_services (skips on backends without get_services).
- Provides discover_yeelight_lamps() for config_flow.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import struct
from typing import Any, Callable

from bleak import BleakClient, BleakError
from bleak.backends.client import BaseBleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    ble_device_has_changed,
    establish_connection,
    BleakNotFoundError,
)

# ---- BLE UUIDs / Protocol constants ----
NOTIFY_UUID = "8f65073d-9f57-4aaa-afea-397d19d5bbeb"
CONTROL_UUID = "aa7d3f34-2d4f-41e0-807f-52fbf8cf7443"

COMMAND_STX = 0x43

CMD_PAIR = 0x67
CMD_PAIR_ON = 0x02
RES_PAIR = 0x63

CMD_POWER = 0x40
CMD_POWER_ON = 0x01
CMD_POWER_OFF = 0x02

CMD_COLOR = 0x41
CMD_BRIGHTNESS = 0x42
CMD_TEMP = 0x43
CMD_RGB = 0x41  # same as CMD_COLOR

CMD_GETSTATE = 0x44
CMD_GETSTATE_SEC = 0x02
RES_GETSTATE = 0x45

CMD_GETNAME = 0x52
RES_GETNAME = 0x53

CMD_GETVER = 0x5C
RES_GETVER = 0x5D

CMD_GETSERIAL = 0x5E
RES_GETSERIAL = 0x5F

RES_GETTIME = 0x62

MODEL_BEDSIDE = "Bedside"
MODEL_CANDELA = "Candela"
MODEL_UNKNOWN = "Unknown"


class Conn(enum.Enum):
    DISCONNECTED = 1
    UNPAIRED = 2
    PAIRING = 3
    PAIRED = 4


_LOGGER = logging.getLogger(__name__)


def model_from_name(ble_name: str | None) -> str:
    name = (ble_name or "").strip()
    if name.startswith("XMCTD_"):
        return MODEL_BEDSIDE
    if name.startswith("yeelight_ms"):
        return MODEL_CANDELA
    return MODEL_UNKNOWN


class Lamp:
    """Yeelight lamp (Candela / Bedside)."""

    MODE_COLOR = 0x01
    MODE_WHITE = 0x02
    MODE_FLOW = 0x03

    def __init__(
        self,
        ble_device: BLEDevice,
        ble_device_callback: Callable[[], BLEDevice | None] | None = None,
    ):
        self._client: BleakClient | None = None
        self._ble_device = ble_device
        self._mac = self._ble_device.address
        self._ble_device_callback = ble_device_callback

        self._is_on = False
        self._rgb = (0, 0, 0)
        self._brightness = 0
        self._temperature = 0

        self.versions: str | None = None
        self._model = model_from_name(self._ble_device.name)
        self._mode: int | None = self.MODE_WHITE if self._model == MODEL_CANDELA else None

        self._state_callbacks: list[Callable[[], None]] = []

        self._conn = Conn.DISCONNECTED
        self._pair_resp_event = asyncio.Event()
        self._read_service = False
        self._is_client_bluez = True  # best-effort hint

        # --- stability controls ---
        self._op_lock = asyncio.Lock()         # serialize all BLE commands
        self._conn_lock = asyncio.Lock()       # serialize connects
        self._notify_started = False           # start_notify only once per session

    # ---- callbacks ----
    def add_callback_on_state_changed(self, func: Callable[[], None]) -> None:
        self._state_callbacks.append(func)

    def run_state_changed_cb(self) -> None:
        for func in self._state_callbacks:
            try:
                func()
            except Exception:
                _LOGGER.debug("State callback raised", exc_info=True)

    def diconnected_cb(self, client: BaseBleakClient) -> None:
        try:
            addr = getattr(client, "address", None)
            _LOGGER.debug("Disconnected CB from client %s", addr if addr else client)
        except Exception:
            _LOGGER.debug("Disconnected CB from client (address unavailable)")
        self._mode = None
        self._conn = Conn.DISCONNECTED
        self._notify_started = False
        self.run_state_changed_cb()

    # ---- connection / pairing ----
    async def connect(self, num_tries: int = 3, timeout: float | None = None, **kwargs) -> None:
        """Connect (idempotent and serialized)."""
        async with self._conn_lock:
            if self._client and self._client.is_connected:
                if self._conn in (Conn.DISCONNECTED, Conn.UNPAIRED):
                    self._conn = Conn.UNPAIRED
                return

            if self._ble_device_callback:
                new_device = self._ble_device_callback()
                if new_device and ble_device_has_changed(self._ble_device, new_device):
                    _LOGGER.debug("BLE device info updated via callback")
                    self._ble_device = new_device

            _LOGGER.debug("Initiating new connection")
            _LOGGER.debug("Connecting now to %s: %s ...", self._mac, self._ble_device.name or "")
            self._client = await establish_connection(
                BleakClient,
                device=self._ble_device,
                name=self._mac,
                disconnected_callback=self.diconnected_cb,
                max_attempts=4,
                ble_device_callback=self._ble_device_callback,
                timeout=timeout or 10.0,
            )

            try:
                self._is_client_bluez = "bluez" in str(type(self._client._backend)).lower()
            except Exception:
                self._is_client_bluez = True

            self._conn = Conn.UNPAIRED
            _LOGGER.debug("Connected: %s", self._client.is_connected)

            # Only try to read services if backend supports it
            if (not self._read_service) and _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    if hasattr(self._client, "get_services"):
                        await self.read_services()
                except Exception as err:
                    _LOGGER.debug("read_services skipped: %s", err)
                self._read_service = True

    async def ensure_paired(self) -> None:
        """Ensure connected & paired (no op_lock here; send_cmd() handles serialization)."""
        await self.connect()

        if not self._client:
            raise BleakError("No client after connect()")

        # Start notifications once per session
        if not self._notify_started:
            try:
                await self._client.start_notify(NOTIFY_UUID, self.notification_handler)
                _LOGGER.debug("Notifications started on %s", NOTIFY_UUID)
                self._notify_started = True
            except Exception as err:
                _LOGGER.debug("start_notify failed/unsupported: %s", err)
                # Some firmwares don't require notify for pairing; continue

        if self._conn == Conn.PAIRED:
            return

        # Attempt pairing (write w/o response)
        await self.pair()

        if self._conn != Conn.PAIRED and self._model == MODEL_CANDELA:
            _LOGGER.error("If this is your first pairing, press the Candela's small button now.")
            try:
                await asyncio.wait_for(self._pair_resp_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

        if self._conn != Conn.PAIRED:
            # Some Candelas never notify pair result; assume paired after write.
            self._conn = Conn.PAIRED

        self.run_state_changed_cb()

    async def disconnect(self) -> None:
        if not self._client:
            return
        try:
            await self._client.disconnect()
        except Exception as err:
            _LOGGER.debug("Disconnect error: %s", err)
        finally:
            self._conn = Conn.DISCONNECTED
            self._notify_started = False
            self.run_state_changed_cb()

    async def pair(self) -> None:
        """Send pairing command. Candela often needs write w/o response."""
        if not self._client:
            raise BleakError("pair() without client")

        bits = bytearray(struct.pack("BBB15x", COMMAND_STX, CMD_PAIR, CMD_PAIR_ON))
        if self._conn not in (Conn.UNPAIRED, Conn.PAIRING):
            return

        try:
            self._pair_resp_event.clear()
            await self._client.write_gatt_char(CONTROL_UUID, bits, response=False)

            # If Bedside (or any device that notifies pair result), wait briefly
            if self._model == MODEL_BEDSIDE:
                try:
                    await asyncio.wait_for(self._pair_resp_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    _LOGGER.debug("Pair wait timed out; may still be paired")
        except Exception as err:
            _LOGGER.debug("Pair write failed: %s", err)

    # ---- debug helper ----
    async def read_services(self) -> None:
        if not self._client:
            return
        try:
            services = await self._client.get_services()
        except Exception as err:
            _LOGGER.debug("read_services failed: %s", err)
            return

        for svc in services:
            _LOGGER.debug("__Service__ %s", svc)
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                try:
                    val = await self._client.read_gatt_char(ch.uuid)
                except Exception as err:
                    val = f"{err}"
                _LOGGER.debug(
                    "__[Characteristic] %s (Handle: %s): %s, Value: %s",
                    ch.uuid,
                    getattr(ch, "handle", "n/a"),
                    props if props else "Unknown",
                    val,
                )
                for dsc in ch.descriptors:
                    try:
                        dval = await self._client.read_gatt_descriptor(dsc.handle)
                    except Exception as err:
                        dval = f"{err}"
                    _LOGGER.debug(
                        "____[Descriptor] %s (Handle: %s): %s) | Value: %s",
                        dsc.uuid,
                        getattr(dsc, "handle", "n/a"),
                        "Characteristic User Description",
                        dval,
                    )

    # ---- public properties ----
    @property
    def mac(self) -> str:
        return self._mac

    @property
    def available(self) -> bool:
        return self._conn == Conn.PAIRED

    @property
    def model(self) -> str:
        return self._model

    @property
    def mode(self) -> int | None:
        return self._mode

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def temperature(self) -> int:
        return self._temperature

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def color(self) -> tuple[int, int, int]:
        return self._rgb

    def get_prop_min_max(self) -> dict[str, Any]:
        return {
            "brightness": {"min": 0, "max": 100},
            "temperature": {"min": 1700, "max": 6500},
            "color": {"min": 0, "max": 255},
        }

    # ---- low-level send + high-level commands ----
    async def send_cmd(self, bits: bytes, wait_notif: float = 0.2) -> bool:
        """Ensure paired, then write (no response); serialized to avoid overlap."""
        async with self._op_lock:
            try:
                await self.ensure_paired()
                if not self._client:
                    return False

                async def _write_once():
                    await self._client.write_gatt_char(CONTROL_UUID, bytearray(bits), response=False)

                try:
                    await _write_once()
                except Exception as err:
                    _LOGGER.debug("First write failed (%s); reconnecting once", err)
                    await self.disconnect()
                    await self.connect()
                    await _write_once()

                if wait_notif:
                    await asyncio.sleep(wait_notif)
                return True
            except Exception as err:
                _LOGGER.debug("Send Cmd error: %s", err)
                return False

    async def get_state(self) -> None:
        bits = struct.pack("BBB15x", COMMAND_STX, CMD_GETSTATE, CMD_GETSTATE_SEC)
        _LOGGER.debug("Send Cmd: Get_state")
        await self.send_cmd(bits)

    async def turn_on(self) -> None:
        bits = struct.pack("BBB15x", COMMAND_STX, CMD_POWER, CMD_POWER_ON)
        _LOGGER.debug("Send Cmd: Turn On")
        ok = await self.send_cmd(bits, wait_notif=0.2)
        if ok:
            self._is_on = True
            self.run_state_changed_cb()

    async def turn_off(self) -> None:
        bits = struct.pack("BBB15x", COMMAND_STX, CMD_POWER, CMD_POWER_OFF)
        _LOGGER.debug("Send Cmd: Turn Off")
        ok = await self.send_cmd(bits, wait_notif=0.2)
        if ok:
            self._is_on = False
            self.run_state_changed_cb()

    async def set_brightness(self, brightness: int) -> None:
        brightness = min(100, max(0, int(brightness)))
        _LOGGER.debug("Set_brightness %s", brightness)
        bits = struct.pack("BBB15x", COMMAND_STX, CMD_BRIGHTNESS, brightness)
        _LOGGER.debug("Send Cmd: Brightness")
        ok = await self.send_cmd(bits, wait_notif=0.0)
        if ok:
            self._brightness = brightness
            self.run_state_changed_cb()

    async def set_temperature(self, kelvin: int, brightness: int | None = None) -> None:
        if brightness is None:
            brightness = self._brightness
        kelvin = min(6500, max(1700, int(kelvin)))
        _LOGGER.debug("Set_temperature %s, %s", kelvin, brightness)
        bits = struct.pack(">BBhB13x", COMMAND_STX, CMD_TEMP, kelvin, brightness)
        _LOGGER.debug("Send Cmd: Temperature")
        ok = await self.send_cmd(bits, wait_notif=0.0)
        if ok:
            self._temperature = kelvin
            self._brightness = brightness
            self._mode = self.MODE_WHITE
            self.run_state_changed_cb()

    async def set_color(
        self, red: int, green: int, blue: int, brightness: int | None = None
    ) -> None:
        if brightness is None:
            brightness = self._brightness
        _LOGGER.debug("Set_color (%s,%s,%s), %s", red, green, blue, brightness)
        bits = struct.pack(
            "BBBBBBB11x",
            COMMAND_STX,
            CMD_RGB,
            red,
            green,
            blue,
            0x01,
            brightness,
        )
        _LOGGER.debug("Send Cmd: Color")
        ok = await self.send_cmd(bits, wait_notif=0.0)
        if ok:
            self._rgb = (red, green, blue)
            self._brightness = brightness
            self._mode = self.MODE_COLOR
            self.run_state_changed_cb()

    # ---- notifications ----
    def notification_handler(self, cHandle: int, data: bytearray) -> None:
        _LOGGER.debug("Received 0x%s from handle=%s", data.hex(), cHandle)
        try:
            res_type = struct.unpack("xB16x", data)[0]
        except Exception:
            return

        if res_type == RES_GETSTATE:
            # Layout differs across models; keep original unpack
            try:
                state = struct.unpack(">xxBBBBBBBhx6x", data)
            except Exception:
                return

            self._is_on = state[0] == CMD_POWER_ON
            if self._model == MODEL_CANDELA:
                self._brightness = state[1]
                self._mode = state[2] if self._conn == Conn.PAIRED else None
            else:
                self._mode = state[1] if self._conn == Conn.PAIRED else None
                self._rgb = (state[2], state[3], state[4])
                self._brightness = state[6]
                self._temperature = state[7]

            _LOGGER.debug("%s", self)
            self.run_state_changed_cb()

        elif res_type == RES_PAIR:
            try:
                pair_mode = struct.unpack("xxB15x", data)[0]
            except Exception:
                return

            if pair_mode == 0x01:
                _LOGGER.error(
                    "Yeelight pairing request: press the lamp's small button to confirm."
                )
                self._mode = None
                self._conn = Conn.PAIRING
                self._pair_resp_event.set()
            elif pair_mode in (0x02, 0x04):
                # success / already paired
                _LOGGER.debug("Yeelight pairing successful/already paired")
                self._conn = Conn.PAIRED
                self._pair_resp_event.set()
            elif pair_mode == 0x03:
                _LOGGER.error("Yeelight not paired; will attempt again on next command.")
                self._mode = None
                self._conn = Conn.UNPAIRED
                self._pair_resp_event.set()
            else:
                _LOGGER.error("Unexpected pairing code: 0x%02x", pair_mode)
                self._mode = None
                self._conn = Conn.UNPAIRED
                self._pair_resp_event.set()


# ---- discovery used by config_flow ----
async def discover_yeelight_lamps(timeout: float = 5.0) -> list[BLEDevice]:
    """Quick Bleak scan for 'yeelight_ms*' (Candela) and 'XMCTD_*' (Bedside)."""
    try:
        from bleak import BleakScanner

        devices = await BleakScanner.discover(timeout=timeout)
        out: list[BLEDevice] = []
        for d in devices:
            name = (d.name or "").strip()
            if name.startswith("yeelight_ms") or name.startswith("XMCTD_"):
                out.append(d)
        return out
    except Exception as err:
        _LOGGER.debug("discover_yeelight_lamps failed: %s", err)
        return []
