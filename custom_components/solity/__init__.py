"""The Smart Solity Doorlock integration."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolityClient
from .calibration import SolityCalibration
from .const import (
    CONF_AUTO_CALIBRATE,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_FIRST_VERIFY_SECONDS,
    CONF_HASHED_PWD,
    CONF_LOG_SECONDS,
    CONF_STATUS_MINUTES,
    DEFAULT_AUTO_CALIBRATE,
    DEFAULT_FIRST_VERIFY_SECONDS,
    DEFAULT_LOG_SECONDS,
    DEFAULT_STATUS_MINUTES,
)
from .coordinator import SolityLogCoordinator, SolityStatusCoordinator

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.SENSOR, Platform.EVENT]


@dataclass
class SolityData:
    """Runtime data: the two coordinators sharing one API client."""

    status: SolityStatusCoordinator
    log: SolityLogCoordinator


type SolityConfigEntry = ConfigEntry[SolityData]


async def async_setup_entry(hass: HomeAssistant, entry: SolityConfigEntry) -> bool:
    """Set up Smart Solity from a config entry."""
    session = async_get_clientsession(hass)
    client = SolityClient(session, entry.data[CONF_EMAIL], entry.data[CONF_HASHED_PWD])
    device_id = entry.data[CONF_DEVICE_ID]

    log_seconds = entry.options.get(CONF_LOG_SECONDS, DEFAULT_LOG_SECONDS)
    status_minutes = entry.options.get(CONF_STATUS_MINUTES, DEFAULT_STATUS_MINUTES)

    calibration = SolityCalibration(hass, entry.entry_id)
    await calibration.async_load()

    status = SolityStatusCoordinator(
        hass,
        entry,
        client,
        device_id,
        status_minutes,
        calibration,
        auto_calibrate=entry.options.get(CONF_AUTO_CALIBRATE, DEFAULT_AUTO_CALIBRATE),
        manual_first=entry.options.get(
            CONF_FIRST_VERIFY_SECONDS, DEFAULT_FIRST_VERIFY_SECONDS
        ),
    )
    log = SolityLogCoordinator(hass, entry, client, device_id, log_seconds)

    await status.async_config_entry_first_refresh()
    await log.async_config_entry_first_refresh()

    entry.runtime_data = SolityData(status=status, log=log)
    # New open in the event log -> invalidate cached deadbolt + verify it.
    entry.async_on_unload(status.attach_log(log))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolityConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SolityConfigEntry) -> None:
    """Drop the stored calibration when the integration is removed."""
    await SolityCalibration(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: SolityConfigEntry) -> None:
    """Reload the entry when options (poll intervals) change."""
    await hass.config_entries.async_reload(entry.entry_id)
