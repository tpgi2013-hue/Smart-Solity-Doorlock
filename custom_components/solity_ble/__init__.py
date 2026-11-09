"""The Solity BLE (direct) integration — open a Solity lock over BLE only.

A standalone, cloud-free companion to the cloud ``solity`` integration: it
talks to the lock's Bluetooth pack directly through Home Assistant's
Bluetooth proxy mesh, so it keeps working (and stays fast) even when the
WiFi+ module / Solity cloud is down. It also reads status (battery, deadbolt,
registered-credential counts, system mode) straight from the lock over BLE.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .ble import SolityBleClient, parse_app_key
from .const import (
    CONF_APP_KEY,
    CONF_MAC,
    CONF_NAME,
    CONF_STATUS_MINUTES,
    DEFAULT_STATUS_MINUTES,
)
from .coordinator import SolityBleCoordinator

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.SENSOR]


@dataclass
class SolityBleData:
    """Runtime data: the BLE client and the status coordinator."""

    client: SolityBleClient
    coordinator: SolityBleCoordinator


type SolityBleConfigEntry = ConfigEntry[SolityBleData]


async def async_setup_entry(
    hass: HomeAssistant, entry: SolityBleConfigEntry
) -> bool:
    """Set up Solity BLE from a config entry."""
    app_key = parse_app_key(entry.data[CONF_APP_KEY])
    client = SolityBleClient(hass, entry.data[CONF_MAC], app_key)

    poll_minutes = entry.options.get(CONF_STATUS_MINUTES, DEFAULT_STATUS_MINUTES)
    coordinator = SolityBleCoordinator(
        hass, client, entry.data.get(CONF_NAME) or "Solity Doorlock (BLE)", poll_minutes
    )
    entry.runtime_data = SolityBleData(client=client, coordinator=coordinator)

    # Don't block setup on the lock being reachable right now (it may be
    # asleep / out of range); refresh in the background instead.
    await coordinator.async_request_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: SolityBleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: SolityBleConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
