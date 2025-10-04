from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.bluetooth import async_ble_device_from_address

try:
    from .const import DOMAIN  # type: ignore
except Exception:
    DOMAIN = "yeelight_bt"

from bleak.backends.device import BLEDevice
from .yeelightbt import Lamp as YeelightBT  # your fork: device class is Lamp

_LOGGER = logging.getLogger(__name__)


# ---------------- helpers ----------------
def _looks_like_esphome_proxy(dev: BLEDevice) -> bool:
    det = getattr(dev, "details", None)
    return det is not None and "bleak_esphome" in str(type(det)).lower()


def _extract_address_type(dev: BLEDevice) -> str | None:
    det = getattr(dev, "details", None)
    if det is None:
        return None
    if isinstance(det, dict):
        at = det.get("address_type") or det.get("addr_type") or det.get("type")
        return str(at) if at else None
    for attr in ("address_type", "addr_type", "type"):
        if hasattr(det, attr):
            val = getattr(det, attr, None)
            if val:
                return str(val)
    return None


def _rebuild_local_bledevice(dev: BLEDevice) -> BLEDevice:
    address = getattr(dev, "address", None)
    name = getattr(dev, "name", None) or "Candela"
    addr_type = _extract_address_type(dev)
    details = {"address_type": addr_type} if addr_type else None
    return BLEDevice(address=address, name=name, details=details, rssi=-60)


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


# ---------------- setup ----------------
async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Yeelight BT light from a ConfigEntry."""

    store = hass.data.setdefault(DOMAIN, {})
    ble_or_container = store.get(entry.entry_id)

    ble_dev: BLEDevice | None = None
    if isinstance(ble_or_container, BLEDevice):
        ble_dev = ble_or_container
    elif isinstance(ble_or_container, dict):
        cand = ble_or_container.get("ble_device") or ble_or_container.get("device")
        if isinstance(cand, BLEDevice):
            ble_dev = cand

    if ble_dev is None:
        mac = entry.data.get("mac") or entry.data.get("address")
        if not mac:
            raise ValueError("Yeelight BT: missing address")
        resolved = async_ble_device_from_address(hass, mac, connectable=True)
        if not resolved:
            raise ValueError(f"Yeelight BT: address {mac} not present in HA bluetooth registry")
        ble_dev = resolved
    else:
        reg = async_ble_device_from_address(hass, ble_dev.address, connectable=True)
        if reg:
            ble_dev = reg

    if _looks_like_esphome_proxy(ble_dev):
        _LOGGER.debug(
            "Yeelight BT: ESPHome proxy backend detected for %s; forcing local adapter",
            getattr(ble_dev, "address", None),
        )
        ble_dev = _rebuild_local_bledevice(ble_dev)
    else:
        _LOGGER.debug(
            "Yeelight BT: Using local adapter for %s (details=%r)",
            getattr(ble_dev, "address", None),
            type(getattr(ble_dev, "details", None)),
        )

    dev = YeelightBT(ble_dev)
    entity = YeelightBTLight(dev, ble_dev, entry)
    async_add_entities([entity], update_before_add=False)


# ---------------- entity ----------------
class YeelightBTLight(LightEntity):
    """Yeelight Bluetooth Candela light (brightness-only)."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_should_poll = True
    _attr_has_entity_name = True

    def __init__(self, device: YeelightBT, ble: BLEDevice, entry: ConfigEntry) -> None:
        self._dev = device
        self._ble = ble
        self._entry = entry

        name = getattr(device, "name", None) or entry.title or "Candela"
        self._attr_name = name

        addr = getattr(ble, "address", None) or getattr(device, "mac", None) or name
        self._unique_addr = addr.replace(":", "-") if isinstance(addr, str) else str(addr)
        self._attr_unique_id = f"{DOMAIN}-{self._unique_addr}-light"

        self._is_on: bool = False
        self._brightness: int | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._unique_addr)},
            "manufacturer": "Yeelight",
            "model": "Candela",
            "name": self._attr_name,
        }

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        return self._brightness

    async def _ensure_connected(self, timeout: float = 6.0) -> None:
        if hasattr(self._dev, "connect"):
            await asyncio.wait_for(_maybe_await(self._dev.connect(timeout=timeout)), timeout=timeout)

    async def async_turn_on(self, **kwargs: Any) -> None:
        kwargs.pop("hs_color", None)
        kwargs.pop("rgb_color", None)
        kwargs.pop("rgbw_color", None)
        kwargs.pop("rgbww_color", None)
        kwargs.pop("xy_color", None)
        kwargs.pop("color_temp", None)
        kwargs.pop("color_temp_kelvin", None)

        try:
            await self._ensure_connected()

            if ATTR_BRIGHTNESS in kwargs and kwargs[ATTR_BRIGHTNESS] is not None:
                level_255 = int(kwargs[ATTR_BRIGHTNESS])
                level_100 = max(1, min(100, round(level_255 * 100 / 255)))
                if hasattr(self._dev, "set_brightness"):
                    await asyncio.wait_for(_maybe_await(self._dev.set_brightness(int(level_100))), timeout=8.0)
                    await asyncio.wait_for(_maybe_await(self._dev.turn_on()), timeout=8.0)
                else:
                    try:
                        await asyncio.wait_for(_maybe_await(self._dev.turn_on(level=int(level_100))), timeout=5.0)
                    except TypeError:
                        await asyncio.wait_for(_maybe_await(self._dev.turn_on()), timeout=8.0)
                self._brightness = level_255
            else:
                await asyncio.wait_for(_maybe_await(self._dev.turn_on()), timeout=8.0)

            self._is_on = True
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self.async_write_ha_state()
        except asyncio.TimeoutError:
            _LOGGER.warning("Yeelight BT: turn_on timed out")
        except Exception as ex:
            _LOGGER.warning("Yeelight BT: turn_on failed: %s", ex)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._ensure_connected()
            await asyncio.wait_for(_maybe_await(self._dev.turn_off()), timeout=5.0)
            self._is_on = False
            self.async_write_ha_state()
        except asyncio.TimeoutError:
            _LOGGER.warning("Yeelight BT: turn_off timed out")
        except Exception as ex:
            _LOGGER.warning("Yeelight BT: turn_off failed: %s", ex)

    async def async_update(self) -> None:
        try:
            async def _read():
                await self._ensure_connected(timeout=3.0)
                st = None
                if hasattr(self._dev, "get_state"):
                    st = await _maybe_await(self._dev.get_state())
                if isinstance(st, dict):
                    if "is_on" in st:
                        self._is_on = bool(st["is_on"])
                    if "brightness" in st and st["brightness"] is not None:
                        br = int(st["brightness"])
                        self._brightness = int(round(max(0, min(100, br)) * 255 / 100))
                else:
                    if hasattr(self._dev, "is_on"):
                        self._is_on = bool(self._dev.is_on)
                    if getattr(self._dev, "brightness", None) is not None:
                        br = int(self._dev.brightness)
                        self._brightness = int(round(max(0, min(100, br)) * 255 / 100))

            await asyncio.wait_for(_read(), timeout=3.0)
            self._attr_color_mode = ColorMode.BRIGHTNESS
        except Exception:
            pass
