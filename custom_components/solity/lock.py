"""Lock platform for Smart Solity.

The lock state is the REAL deadbolt (motor) state from ``get_status`` — never
a timer guess. The status coordinator invalidates it to 'unlocked' as soon as
an open appears in the event log (or is sent from HA) and then re-polls the
lock in a short backoff burst until the deadbolt reads locked again.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SolityConfigEntry
from .const import CONF_DEVICE_ID, CONF_NICKNAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import SolityStatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity lock."""
    async_add_entities([SolityLock(entry.runtime_data.status, entry)])


class SolityLock(CoordinatorEntity[SolityStatusCoordinator], LockEntity):
    """A Solity door lock controlled through the cloud API."""

    _attr_has_entity_name = True
    _attr_name = None  # use the device name
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(
        self, coordinator: SolityStatusCoordinator, entry: SolityConfigEntry
    ) -> None:
        super().__init__(coordinator)
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=entry.data.get(CONF_NICKNAME) or "Solity Doorlock",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_locked(self) -> bool | None:
        """Real deadbolt state (1 = locked)."""
        return self.coordinator.is_locked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "sub_latch": data.get("subLatch"),
            "system_mode": data.get("systemMode"),
            "card_count": data.get("cardCount"),
            "password_count": data.get("passwordCount"),
            "fingerprint_count": data.get("fingerPrintCount"),
            # True while post-open status polls are still waiting for the bolt.
            "verifying": self.coordinator.verifying,
            # False = value is assumed (open seen / poll missed), not read back.
            "confirmed": self.coordinator.last_poll_ok,
            # Latest post-open verify: seconds after the open was detected and
            # how many status polls it took (tuning data for the offsets).
            "last_verify_seconds": self.coordinator.last_verify_seconds,
            "last_verify_polls": self.coordinator.last_verify_polls,
            "last_verify_result": self.coordinator.last_verify_result,
            # Measurement mode only: timing of the latest relock (see README).
            "measure_mode": self.coordinator.measure_mode,
            "last_measurement": self.coordinator.last_measurement,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        await self.coordinator.client.close(self.coordinator.device_id)
        self.coordinator.start_verify()

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.coordinator.client.open(self.coordinator.device_id)
        self.coordinator.mark_unlocked()
        self.coordinator.start_verify(dt_util.utcnow())

    async def async_open(self, **kwargs: Any) -> None:
        await self.async_unlock(**kwargs)
