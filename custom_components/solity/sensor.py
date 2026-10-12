"""Sensor platform for Smart Solity (battery + last access)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolityConfigEntry
from .const import CONF_DEVICE_ID, CONF_NICKNAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import SolityLogCoordinator, SolityStatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity sensors."""
    data = entry.runtime_data
    async_add_entities(
        [
            SolityBatterySensor(data.status, entry),
            SolityLastAccessSensor(data.log, entry),
        ]
    )


def _device_info(entry: SolityConfigEntry) -> DeviceInfo:
    device_id = entry.data[CONF_DEVICE_ID]
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=entry.data.get(CONF_NICKNAME) or "Solity Doorlock",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class SolityBatterySensor(CoordinatorEntity[SolityStatusCoordinator], SensorEntity):
    """Battery level reported by the door lock."""

    _attr_has_entity_name = True
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolityStatusCoordinator, entry: SolityConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_battery"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        value = (self.coordinator.data or {}).get("battery")
        return int(value) if value is not None else None


class SolityLastAccessSensor(CoordinatorEntity[SolityLogCoordinator], SensorEntity):
    """Most recent access-log entry (who / how / when)."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_access"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: SolityLogCoordinator, entry: SolityConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_last_access"
        self._attr_device_info = _device_info(entry)

    @property
    def _latest(self) -> dict | None:
        logs = self.coordinator.data or []
        return logs[0] if logs else None

    @property
    def native_value(self) -> str | None:
        entry = self._latest
        if not entry:
            return None
        # Human-readable message if present, else compose who/how.
        msg = entry.get("logMessage")
        if msg:
            return msg[:255]
        who = entry.get("nickname") or ""
        how = entry.get("mediaType") or ""
        return (f"{who} {how}".strip() or None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry = self._latest or {}
        return {
            "datetime": entry.get("logDateTime"),
            "who": entry.get("nickname"),
            "method": entry.get("mediaType"),
            "log_type": entry.get("logType"),
            "log_code": entry.get("logCode"),
            "message": entry.get("logMessage"),
        }
