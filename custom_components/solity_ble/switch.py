"""Switch platform for Solity BLE: hold a prewarmed BLE session.

On: connect + authenticate to the lock now and keep the session, so an open
that follows (face recognition a few seconds after presence) skips the BLE
connection setup. Off: drop the session at once. The session also ends after
an open/close, or after PREWARM_MAX_SECONDS as a safety cap; the switch then
turns itself off.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolityBleConfigEntry
from .ble import SolityBleError
from .const import CONF_MAC, DOMAIN, PREWARM_MAX_SECONDS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity BLE prewarm switch."""
    async_add_entities([SolityBlePrewarmSwitch(entry)])


class SolityBlePrewarmSwitch(SwitchEntity):
    """Hold a connected + authenticated BLE session while on."""

    _attr_has_entity_name = True
    _attr_translation_key = "prewarm"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_should_poll = False

    def __init__(self, entry: SolityBleConfigEntry) -> None:
        self._client = entry.runtime_data.client
        mac = entry.data[CONF_MAC]
        self._attr_unique_id = f"{mac}_ble_prewarm_switch"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, mac)})

    @property
    def is_on(self) -> bool:
        return self._client.holding

    async def async_added_to_hass(self) -> None:
        self._client.set_hold_listener(self._hold_changed)

    async def async_will_remove_from_hass(self) -> None:
        self._client.set_hold_listener(None)

    @callback
    def _hold_changed(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._client.async_prewarm(PREWARM_MAX_SECONDS)
        except SolityBleError as err:
            raise HomeAssistantError(f"BLE 예열 실패: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._client.async_release()
