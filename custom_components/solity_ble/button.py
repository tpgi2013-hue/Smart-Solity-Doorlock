"""Button platform for Solity BLE: prewarm the BLE session.

Pressing it connects and authenticates to the lock ahead of time and holds the
session for a short window, so an open that follows right after (e.g. face
recognition a few seconds after presence is detected) skips the ~3 s BLE
connection setup.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolityBleConfigEntry
from .ble import SolityBleError
from .const import CONF_MAC, DOMAIN, PREWARM_HOLD_SECONDS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity BLE prewarm button."""
    async_add_entities([SolityBlePrewarmButton(entry)])


class SolityBlePrewarmButton(ButtonEntity):
    """Connect + authenticate now and hold the session briefly."""

    _attr_has_entity_name = True
    _attr_translation_key = "prewarm"
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, entry: SolityBleConfigEntry) -> None:
        self._client = entry.runtime_data.client
        mac = entry.data[CONF_MAC]
        self._attr_unique_id = f"{mac}_ble_prewarm"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, mac)})

    async def async_press(self) -> None:
        try:
            await self._client.async_prewarm(PREWARM_HOLD_SECONDS)
        except SolityBleError as err:
            raise HomeAssistantError(f"BLE 예열 실패: {err}") from err
