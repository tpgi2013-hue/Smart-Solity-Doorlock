"""Lock platform for Solity BLE (direct).

There is no state feedback over this BLE path (open/close only), so the
deadbolt state is optimistic: a successful open shows 'unlocked' for a short
window (the lock's own auto-relock time) and then returns to 'locked'; a
successful lock shows 'locked'. The entity is always available — opening over
BLE must work even when the cloud side is down, which is the whole point.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import SolityBleConfigEntry
from .ble import SolityBleClient, SolityBleError
from .const import (
    CONF_MAC,
    CONF_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    OPTIMISTIC_UNLOCK_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity BLE lock."""
    async_add_entities([SolityBleLock(entry)])


class SolityBleLock(LockEntity):
    """A Solity door lock opened directly over BLE."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, entry: SolityBleConfigEntry) -> None:
        self._client: SolityBleClient = entry.runtime_data.client
        mac = entry.data[CONF_MAC]
        self._attr_unique_id = f"{mac}_ble_lock"
        self._attr_is_locked = True  # optimistic default
        self._cancel_relock = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={("bluetooth", mac)},
            name=entry.data.get(CONF_NAME) or "Solity Doorlock (BLE)",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        # Always available: BLE open must work regardless of cloud state.
        return True

    @callback
    def _clear_relock(self) -> None:
        if self._cancel_relock is not None:
            self._cancel_relock()
            self._cancel_relock = None

    async def async_unlock(self, **kwargs: Any) -> None:
        """Retract the deadbolt over BLE (optimistic)."""
        try:
            await self._client.async_open()
        except SolityBleError as err:
            _LOGGER.warning("Solity BLE open failed: %s", err)
            raise HomeAssistantError(f"BLE 열림 실패: {err}") from err

        self._clear_relock()
        self._attr_is_locked = False
        self.async_write_ha_state()

        @callback
        def _relock(_now) -> None:
            self._cancel_relock = None
            self._attr_is_locked = True
            self.async_write_ha_state()

        self._cancel_relock = async_call_later(
            self.hass, OPTIMISTIC_UNLOCK_SECONDS, _relock
        )

    async def async_open(self, **kwargs: Any) -> None:
        await self.async_unlock(**kwargs)

    async def async_lock(self, **kwargs: Any) -> None:
        """Throw the deadbolt over BLE (optimistic)."""
        try:
            await self._client.async_close()
        except SolityBleError as err:
            _LOGGER.warning("Solity BLE close failed: %s", err)
            raise HomeAssistantError(f"BLE 잠금 실패: {err}") from err
        self._clear_relock()
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._clear_relock()
