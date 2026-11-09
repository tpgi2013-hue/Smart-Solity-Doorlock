"""Sensor platform for Solity BLE — everything the status frame reports."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolityBleConfigEntry
from .ble import SolityStatus
from .const import CONF_MAC, CONF_NAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import SolityBleCoordinator


@dataclass(frozen=True, kw_only=True)
class SolityBleSensorDescription(SensorEntityDescription):
    """Describes a Solity BLE sensor."""

    value_fn: Callable[[SolityStatus], int | None]


SENSORS: tuple[SolityBleSensorDescription, ...] = (
    SolityBleSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery,
    ),
    SolityBleSensorDescription(
        key="count_card",
        translation_key="count_card",
        icon="mdi:card-account-details",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.count_card,
    ),
    SolityBleSensorDescription(
        key="count_password",
        translation_key="count_password",
        icon="mdi:dialpad",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.pw_info,
    ),
    # No count_fingerprint / count_face / count_vein here: this lock's BLE
    # status frame reports password and card counts, but not fingerprint, face
    # or vein (the app reads those from a separate registration-list command).
    # Use the FM225 entity (sensor.front_door_face_face_count) for faces.
    SolityBleSensorDescription(
        key="count_remote",
        translation_key="count_remote",
        icon="mdi:remote",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.count_remote,
    ),
    SolityBleSensorDescription(
        key="count_mastercard",
        translation_key="count_mastercard",
        icon="mdi:card-account-details-star",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.count_mastercard,
    ),
    SolityBleSensorDescription(
        key="system_mode",
        translation_key="system_mode",
        icon="mdi:cog",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.system_mode,
    ),
    SolityBleSensorDescription(
        key="sub_latch",
        translation_key="sub_latch",
        icon="mdi:gesture-tap-button",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.sub_latch,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolityBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Solity BLE sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        SolityBleSensor(coordinator, entry, desc) for desc in SENSORS
    ]
    entities.append(SolityBleRssiSensor(hass, entry))
    async_add_entities(entities)


def _device_info(entry: SolityBleConfigEntry) -> DeviceInfo:
    mac = entry.data[CONF_MAC]
    return DeviceInfo(
        identifiers={(DOMAIN, mac)},
        connections={("bluetooth", mac)},
        name=entry.data.get(CONF_NAME) or "Solity Doorlock (BLE)",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class SolityBleRssiSensor(SensorEntity):
    """Signal strength of the lock as seen by the HA Bluetooth proxy mesh."""

    _attr_has_entity_name = True
    _attr_translation_key = "rssi"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: SolityBleConfigEntry) -> None:
        self._hass = hass
        self._mac = entry.data[CONF_MAC]
        self._attr_unique_id = f"{self._mac}_rssi"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int | None:
        # connectable=False: the strongest advertisement across all proxies.
        info = async_last_service_info(self._hass, self._mac, connectable=False)
        return info.rssi if info is not None else None


class SolityBleSensor(CoordinatorEntity[SolityBleCoordinator], SensorEntity):
    """A status value read from the lock over BLE."""

    _attr_has_entity_name = True
    entity_description: SolityBleSensorDescription

    def __init__(
        self,
        coordinator: SolityBleCoordinator,
        entry: SolityBleConfigEntry,
        description: SolityBleSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        mac = entry.data[CONF_MAC]
        self._attr_unique_id = f"{mac}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={("bluetooth", mac)},
            name=entry.data.get(CONF_NAME) or "Solity Doorlock (BLE)",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data)
