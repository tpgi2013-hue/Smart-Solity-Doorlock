"""Binary sensor platform for Smart Solity: door open/closed.

Read from the push-pull mortise's door-detect latch (``subLatch``) that the
lock reports in get_status. It is only as fresh as the last status poll: it
updates during the post-open checks and on the regular status interval, and
is unknown right after an open until the next reading.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolityConfigEntry
from .const import CONF_DEVICE_ID, CONF_NICKNAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import SolityStatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity door sensor."""
    async_add_entities([SolityDoorSensor(entry.runtime_data.status, entry)])


class SolityDoorSensor(CoordinatorEntity[SolityStatusCoordinator], BinarySensorEntity):
    """Door open (on) / closed (off) from the lock's door-detect latch."""

    _attr_has_entity_name = True
    _attr_translation_key = "door"
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(
        self, coordinator: SolityStatusCoordinator, entry: SolityConfigEntry
    ) -> None:
        super().__init__(coordinator)
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_door"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=entry.data.get(CONF_NICKNAME) or "Solity Doorlock",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.door_open

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # False = value not read back from the lock yet (right after an open).
        return {"confirmed": self.coordinator.last_poll_ok}
