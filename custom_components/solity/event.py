"""Event platform for Smart Solity.

- ``open`` / ``other``: fired for each new access-log entry (from the device).
- ``close``: fired when a status poll reads the deadbolt as locked after it
  was unlocked — i.e. the motor really locked. No timer-based guessing.
"""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolityConfigEntry
from .const import (
    CONF_DEVICE_ID,
    CONF_NICKNAME,
    DOMAIN,
    EVENT_CLOSE,
    EVENT_OPEN,
    EVENT_OTHER,
    EVENT_TYPES,
    LOG_CODE_CLOSE,
    MANUFACTURER,
    MODEL,
    OPEN_LOG_CODES,
    VERIFIED_CLOSE_LOG_TYPE,
    VERIFIED_CLOSE_MESSAGE,
    VERIFIED_CLOSE_METHOD,
)
from .coordinator import SolityLogCoordinator, SolityStatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity door-event entity."""
    data = entry.runtime_data
    async_add_entities([SolityDoorEvent(data.log, data.status, entry)])


def _event_type(log_code: str | None) -> str:
    if log_code in OPEN_LOG_CODES:
        return EVENT_OPEN
    if log_code == LOG_CODE_CLOSE:
        return EVENT_CLOSE
    return EVENT_OTHER


class SolityDoorEvent(CoordinatorEntity[SolityLogCoordinator], EventEntity):
    """Door events: opens from the access log, closes from the real deadbolt."""

    _attr_has_entity_name = True
    _attr_translation_key = "door_event"
    _attr_event_types = EVENT_TYPES
    _attr_icon = "mdi:door"

    def __init__(
        self,
        coordinator: SolityLogCoordinator,
        status: SolityStatusCoordinator,
        entry: SolityConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._status = status
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_door_event"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data.get(CONF_NICKNAME) or "Solity Doorlock",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
        self._last_dt: str | None = None
        self._prev_locked: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Seed baselines so startup does not replay history."""
        await super().async_added_to_hass()
        logs = self.coordinator.data or []
        if logs:
            self._last_dt = logs[0].get("logDateTime")
        self._prev_locked = self._status.is_locked
        self.async_on_remove(
            self._status.async_add_listener(self._handle_status_update)
        )

    @callback
    def _handle_status_update(self) -> None:
        """Fire 'close' on a confirmed unlocked -> locked deadbolt transition."""
        locked = self._status.is_locked
        prev = self._prev_locked
        self._prev_locked = locked
        if prev is False and locked is True and self._status.last_poll_ok:
            self._trigger_event(
                EVENT_CLOSE,
                {
                    "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "who": None,
                    "method": VERIFIED_CLOSE_METHOD,
                    "log_type": VERIFIED_CLOSE_LOG_TYPE,
                    "log_code": LOG_CODE_CLOSE,
                    "message": VERIFIED_CLOSE_MESSAGE,
                },
            )
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        logs = self.coordinator.data or []
        if not logs:
            return

        if self._last_dt is None:
            self._last_dt = logs[0].get("logDateTime")
            super()._handle_coordinator_update()
            return

        # Collect entries newer than the last seen, oldest-first.
        new_entries = [
            log for log in logs if (log.get("logDateTime") or "") > self._last_dt
        ]
        for log in reversed(new_entries):
            self._trigger_event(
                _event_type(log.get("logCode")),
                {
                    "datetime": log.get("logDateTime"),
                    "who": log.get("nickname"),
                    "method": log.get("mediaType"),
                    "log_type": log.get("logType"),
                    "log_code": log.get("logCode"),
                    "message": log.get("logMessage"),
                },
            )

        if new_entries:
            self._last_dt = logs[0].get("logDateTime")

        super()._handle_coordinator_update()
