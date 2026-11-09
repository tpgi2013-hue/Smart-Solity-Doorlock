"""The Solity BLE (direct) integration — open a Solity lock over BLE only.

A standalone, cloud-free companion to the cloud ``solity`` integration: it
talks to the lock's Bluetooth pack directly through Home Assistant's
Bluetooth proxy mesh, so it keeps working (and stays fast) even when the
WiFi+ module / Solity cloud is down.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .ble import SolityBleClient, parse_app_key
from .const import CONF_APP_KEY, CONF_MAC

PLATFORMS: list[Platform] = [Platform.LOCK]


@dataclass
class SolityBleData:
    """Runtime data: the BLE client for this lock."""

    client: SolityBleClient


type SolityBleConfigEntry = ConfigEntry[SolityBleData]


async def async_setup_entry(
    hass: HomeAssistant, entry: SolityBleConfigEntry
) -> bool:
    """Set up Solity BLE from a config entry."""
    app_key = parse_app_key(entry.data[CONF_APP_KEY])
    client = SolityBleClient(hass, entry.data[CONF_MAC], app_key)
    entry.runtime_data = SolityBleData(client=client)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SolityBleConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
