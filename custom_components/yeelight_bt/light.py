""" light platform """
from __future__ import annotations

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

# Local integration modules
from .const import DOMAIN  # existing const in this integration
from .yeelightbt import YeelightBT  # ADAPT_IF_NEEDED: class name in your repo

_LOGGER = logging.getLogger(__name__)

# Home Assistant calls this to set up entities from a ConfigEntry
async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Yeelight BT light from a ConfigEntry."""
    # Most versions of this component stash device instances under hass.data[DOMAIN][entry.entry_id]
    data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})

    # The repo typically stores a YeelightBT device instance at "device"
    dev: YeelightBT | None = data.get("device")  # ADAPT_IF_NEEDED if your key differs

    if dev is None:
        # Fallback: build a device from entry.data if needed
        name = entry.data.get("name") or "Yeelight BT"
        mac = entry.data.get("mac")
        if not mac:
            raise ValueError("Yeelight BT entry missing 'mac' address")

        # ADAPT_IF_NEEDED: if the constructor signature differs in your yeelightbt.py
        dev = YeelightBT(name=name, mac=mac)
        data["device"] = dev

    entity = YeelightBTLight(dev)
    async_add_entities([entity], update_before_add=True)


class YeelightBTLight(LightEntity):
    """Yeelight Bluetooth light entity (Candela brightness-only)."""

    # === Patch 1 (part A): advertise brightness-only ===
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, device: YeelightBT) -> None:
        self._dev = device
        self._attr_name = getattr(device, "name", "Yeelight BT")
        self._attr_unique_id = getattr(device, "mac", None) or self._attr_name
        self._is_on: bool = False
        self._brightness: int | None = None

    @property
    def available(self) -> bool:
        """Entity availability based on device connectivity."""
        # ADAPT_IF_NEEDED: your device may expose .available or .is_connected()
        available = True
        try:
            if hasattr(self._dev, "available"):
                available = bool(self._dev.available)
            elif hasattr(self._dev, "is_connected"):
                available = bool(self._dev.is_connected())
        except Exception:  # noqa: BLE001 - be robust to BLE transport errors
            available = False
        return available

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        return self._brightness

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on light; accept only brightness (strip color inputs)."""

        # === Patch 1 (part B): defensively drop unsupported color keys ===
        kwargs.pop("hs_color", None)
        kwargs.pop("rgb_color", None)
        kwargs.pop("rgbw_color", None)
        kwargs.pop("rgbww_color", None)
        kwargs.pop("xy_color", None)
        kwargs.pop("color_temp", None)
        kwargs.pop("color_temp_kelvin", None)
        # ---------------------------------------------------------------

        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)
        try:
            if brightness is None:
                # ADAPT_IF_NEEDED: some backends require explicit level on power-on
                await self._dev.turn_on()  # e.g. send "power on" only
            else:
                # Normalize: HA uses 0..255
                level = max(1, min(255, int(brightness)))
                # ADAPT_IF_NEEDED: your API may have set_brightness(level) and/or turn_on(level)
                if hasattr(self._dev, "set_brightness"):
                    await self._dev.set_brightness(level)
                    await self._dev.turn_on()
                else:
                    await self._dev.turn_on(level=level)
            self._is_on = True
            if brightness is not None:
                self._brightness = int(brightness)
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self.async_write_ha_state()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.warning("YeelightBT: turn_on failed: %s", ex)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._dev.turn_off()
            self._is_on = False
            self.async_write_ha_state()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.warning("YeelightBT: turn_off failed: %s", ex)

    async def async_update(self) -> None:
        """Poll device state (kept minimal to avoid long BLE holds)."""
        try:
            # ADAPT_IF_NEEDED: many repos expose an async update or a state getter
            if hasattr(self._dev, "async_update"):
                state = await self._dev.async_update()
            elif hasattr(self._dev, "get_state"):
                state = await self._dev.get_state()
            else:
                state = None

            # expected keys: on/off + brightness 0..255 (or 1..100)
            if isinstance(state, dict):
                if "is_on" in state:
                    self._is_on = bool(state["is_on"])
                if "brightness" in state and state["brightness"] is not None:
                    br = state["brightness"]
                    # normalize to 0..255 if backend returns 1..100
                    self._brightness = int(round(br * 2.55)) if br <= 100 else int(br)
                self._attr_color_mode = ColorMode.BRIGHTNESS
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("YeelightBT: update failed (will show as unavailable): %s", ex)
            # Leave last-known state; HA will reflect 'unavailable' via .available
