"""Lock platform for Solity BLE (direct).

The real deadbolt state comes from the lock's status frame (read on a timer
and after every command). Right after an open the entity shows 'unlocked'
optimistically for the lock's auto-relock window, then falls back to the real
status. The entity is always available — opening over BLE must work even when
the cloud side is down, which is the whole point.
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
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolityBleConfigEntry
from .ble import SolityBleError, SolityBleRejected
from .const import (
    CONF_MAC,
    CONF_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    OPTIMISTIC_UNLOCK_SECONDS,
)
from .coordinator import SolityBleCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity BLE lock."""
    async_add_entities([SolityBleLock(entry)])


class SolityBleLock(CoordinatorEntity[SolityBleCoordinator], LockEntity):
    """A Solity door lock opened directly over BLE."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, entry: SolityBleConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._client = entry.runtime_data.client
        mac = entry.data[CONF_MAC]
        self._attr_unique_id = f"{mac}_ble_lock"
        self._optimistic_unlocked = False
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
        # Always available: BLE open must work regardless of cloud/poll state.
        return True

    @property
    def is_locked(self) -> bool | None:
        if self._optimistic_unlocked:
            return False
        data = self.coordinator.data
        if data is not None and data.is_locked is not None:
            return data.is_locked
        return True  # assume locked until a status read says otherwise

    @callback
    def _clear_relock(self) -> None:
        if self._cancel_relock is not None:
            self._cancel_relock()
            self._cancel_relock = None

    async def async_unlock(self, **kwargs: Any) -> None:
        """Retract the deadbolt over BLE (optimistic until auto-relock)."""
        try:
            status = await self._client.async_open()
        except SolityBleError as err:
            _LOGGER.warning("Solity BLE open failed: %s", err)
            if isinstance(err, SolityBleRejected) and await self._real_state_is(False):
                return  # already open (e.g. opened from inside) — nothing to do
            raise HomeAssistantError(f"BLE 열림 실패: {err}") from err

        # The captured status predates the open (battery/counts valid), so keep
        # it for the sensors but show 'unlocked' optimistically for the window.
        self.coordinator.apply_status(status)
        self._clear_relock()
        self._optimistic_unlocked = True
        self.async_write_ha_state()

        @callback
        def _relock(_now) -> None:
            self._cancel_relock = None
            self._optimistic_unlocked = False
            self.async_write_ha_state()
            self.hass.async_create_task(self.coordinator.async_request_refresh())

        self._cancel_relock = async_call_later(
            self.hass, OPTIMISTIC_UNLOCK_SECONDS, _relock
        )

    async def _real_state_is(self, locked: bool) -> bool:
        """After a rejected command, re-read status so the state is real.

        Returns True when the fresh status says the deadbolt is already in the
        wanted position (the command was rejected because nothing to do).
        """
        await self.coordinator.async_refresh()
        data = self.coordinator.data
        return data is not None and data.is_locked is locked

    async def async_open(self, **kwargs: Any) -> None:
        await self.async_unlock(**kwargs)

    async def async_lock(self, **kwargs: Any) -> None:
        """Throw the deadbolt over BLE."""
        try:
            status = await self._client.async_close()
        except SolityBleError as err:
            _LOGGER.warning("Solity BLE close failed: %s", err)
            if isinstance(err, SolityBleRejected) and await self._real_state_is(True):
                self._clear_relock()
                self._optimistic_unlocked = False
                self.async_write_ha_state()
                return  # already locked — the shown state was stale
            raise HomeAssistantError(f"BLE 잠금 실패: {err}") from err
        self._clear_relock()
        self._optimistic_unlocked = False
        self.coordinator.apply_status(status)
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        # A fresh status read corrects the real state once optimism expires.
        if not self._optimistic_unlocked:
            super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._clear_relock()
        self.coordinator.cancel_recheck()
        await super().async_will_remove_from_hass()
