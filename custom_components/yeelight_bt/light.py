from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.components import bluetooth
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo

from .yeelightbt import Lamp

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "yeelight_bt"

# Convert HA 0-255 brightness to Candela 0-100 scale
def _ha_to_pct(ha_bri: int) -> int:
    if ha_bri is None:
        return 100
    return max(1, min(100, round(ha_bri * 100 / 255)))

def _pct_to_ha(pct: int) -> int:
    return max(1, min(255, round(pct * 255 / 100)))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the light platform from a config entry."""
    data = entry.data or {}
    title = entry.title or "Yeelight BT"
    address: str | None = data.get("address") or data.get("mac") or entry.unique_id

    # Try to get a BLEDevice from HA's BT stack (works with ESPHome BT Proxy too)
    ble_device = None
    if address:
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            # As a fallback, try non-connectable cache (rare)
            ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=False)

    # If config flow stored an actual BLEDevice object, accept it too
    if ble_device is None and isinstance(data.get("device"), object):
        candidate = data.get("device")
        if getattr(candidate, "address", None):
            ble_device = candidate

    if ble_device is None:
        _LOGGER.error("Unable to locate BLE device for %s; address=%s", title, address)
        return

    lamp = Lamp(ble_device, ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    ))

    entity = YeelightBTLight(hass, entry, lamp, title, address)
    async_add_entities([entity])


class YeelightBTLight(LightEntity):
    """Home Assistant entity for Yeelight Candela over BLE."""

    _attr_should_poll = True  # we also run a light heartbeat
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, lamp: Lamp, name: str, mac: str):
        self.hass = hass
        self._entry = entry
        self._dev = lamp
        self._attr_name = name
        self._mac = mac
        self._attr_unique_id = mac.lower()

        # Internal state mirrors Lamp (authoritative comes from notifications)
        self._is_on = False
        self._brightness = 255  # HA scale 0-255; Lamp stores 0-100

        # Avoid command/update races
        self._busy_lock = asyncio.Lock()

        # A lightweight heartbeat/poll task to reflect manual changes & availability
        self._poll_task: asyncio.Task | None = None
        self._consec_fail = 0

        # Device info for registry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            manufacturer="Yeelight",
            name=name,
            connections={("bluetooth", mac)},
            model=self._dev.model or "Candela",
        )

        # When lamp notifies state, refresh entity
        self._dev.add_callback_on_state_changed(self._schedule_state_push)

    # ---- entity protocol ----
    async def async_added_to_hass(self) -> None:
        # Immediate first sync so the card becomes controllable quickly
        async def _prime():
            try:
                await asyncio.wait_for(self._dev.get_state(), timeout=6.0)
            except Exception:
                pass
            self._schedule_state_push()

        asyncio.create_task(_prime())

        # Start heartbeat: poll state periodically to catch manual changes
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def async_will_remove_from_hass(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    @property
    def available(self) -> bool:
        """Consider entity available if Lamp recently OK'd OR the device is visible to HA BT."""
        if self._dev.available:
            return True
        # If BT stack currently sees the device (adapter or proxy), allow toggling.
        seen = bluetooth.async_ble_device_from_address(self.hass, self._mac, connectable=True)
        if seen is None:
            seen = bluetooth.async_ble_device_from_address(self.hass, self._mac, connectable=False)
        return seen is not None

    @property
    def is_on(self) -> bool:
        return self._dev.is_on if self._dev else self._is_on

    @property
    def brightness(self) -> int | None:
        return _pct_to_ha(self._dev.brightness) if self._dev else self._brightness

    async def async_turn_on(self, **kwargs: Any) -> None:
        async with self._busy_lock:
            try:
                # Brightness first (wakes the lamp too on Candela)
                if (b := kwargs.get(ATTR_BRIGHTNESS)) is not None:
                    pct = _ha_to_pct(b)
                    await asyncio.wait_for(self._dev.set_brightness(pct), timeout=8.0)
                    self._brightness = b
                    self._is_on = True
                else:
                    await asyncio.wait_for(self._dev.turn_on(), timeout=8.0)
                    self._is_on = True
                # Ask for state to sync (proxy may deliver slightly later)
                await asyncio.wait_for(self._dev.get_state(), timeout=4.0)
                self._consec_fail = 0
            except asyncio.TimeoutError:
                _LOGGER.warning("Yeelight BT: turn_on timed out")
                self._consec_fail += 1
            except Exception as err:
                _LOGGER.warning("Yeelight BT: turn_on failed: %s", err)
                self._consec_fail += 1

            self._schedule_state_push()

    async def async_turn_off(self, **kwargs: Any) -> None:
        async with self._busy_lock:
            try:
                await asyncio.wait_for(self._dev.turn_off(), timeout=8.0)
                self._is_on = False
                await asyncio.wait_for(self._dev.get_state(), timeout=4.0)
                self._consec_fail = 0
            except asyncio.TimeoutError:
                _LOGGER.warning("Yeelight BT: turn_off timed out")
                self._consec_fail += 1
            except Exception as err:
                _LOGGER.warning("Yeelight BT: turn_off failed: %s", err)
                self._consec_fail += 1
            self._schedule_state_push()

    async def async_update(self) -> None:
        # Skip polling if a command is in-flight
        if self._busy_lock.locked():
            return
        try:
            await asyncio.wait_for(self._dev.get_state(), timeout=5.0)
            self._consec_fail = 0
        except Exception:
            # Silent: proxies can be slower; the heartbeat loop tracks availability
            self._consec_fail += 1

    # ---- heartbeat loop to reflect manual changes & availability ----
    async def _poll_loop(self) -> None:
        try:
            while True:
                # If a command is running, skip this tick
                if not self._busy_lock.locked():
                    try:
                        await asyncio.wait_for(self._dev.get_state(), timeout=5.0)
                        self._consec_fail = 0
                    except Exception:
                        self._consec_fail += 1

                    # If repeated failures, just push state so HA marks as unavailable
                    if self._consec_fail >= 3:
                        self._schedule_state_push()

                await asyncio.sleep(20.0)  # poll interval; safe for battery & proxy
        except asyncio.CancelledError:
            return

    # ---- helpers ----
    def _schedule_state_push(self) -> None:
        # Pull from Lamp state
        self._is_on = self._dev.is_on
        self._brightness = _pct_to_ha(self._dev.brightness)
        try:
            self.async_write_ha_state()
        except Exception:
            pass
