"""The Smart Solity Doorlock integration."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import SolityClient
from .const import (
    CONF_API_DEVICE_ID,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_HASHED_PWD,
    CONF_LOG_SECONDS,
    CONF_STATUS_MINUTES,
    DEFAULT_LOG_SECONDS,
    DEFAULT_STATUS_MINUTES,
    DOMAIN,
    ISSUE_SESSION_TAKEN,
    LEGACY_CALIBRATION_STORE,
    LEGACY_ENTITIES,
    LEGACY_OPTION_KEYS,
)
from .coordinator import SolityLogCoordinator, SolityStatusCoordinator

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


@dataclass
class SolityData:
    """Runtime data: the two coordinators sharing one API client."""

    status: SolityStatusCoordinator
    log: SolityLogCoordinator


type SolityConfigEntry = ConfigEntry[SolityData]


async def _async_cleanup_legacy(hass: HomeAssistant, entry: SolityConfigEntry) -> None:
    """Remove what older versions left behind (options, store, entities)."""
    if any(key in entry.options for key in LEGACY_OPTION_KEYS):
        hass.config_entries.async_update_entry(
            entry,
            options={
                k: v for k, v in entry.options.items() if k not in LEGACY_OPTION_KEYS
            },
        )
    await Store(
        hass, 1, f"{DOMAIN}.{LEGACY_CALIBRATION_STORE}.{entry.entry_id}"
    ).async_remove()
    ent_reg = er.async_get(hass)
    for platform, suffix in LEGACY_ENTITIES:
        old = ent_reg.async_get_entity_id(
            platform, DOMAIN, f"{entry.data[CONF_DEVICE_ID]}_{suffix}"
        )
        if old:
            ent_reg.async_remove(old)


def _issue_id(entry: SolityConfigEntry) -> str:
    return f"{ISSUE_SESSION_TAKEN}_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: SolityConfigEntry) -> bool:
    """Set up Smart Solity from a config entry."""
    # Before the update listener is attached, so this does not trigger a reload.
    await _async_cleanup_legacy(hass, entry)

    session = async_get_clientsession(hass)
    client = SolityClient(session, entry.data[CONF_EMAIL], entry.data[CONF_HASHED_PWD])
    # Entities are keyed on CONF_DEVICE_ID; API calls use the id the current
    # account knows the lock by (differs after switching to another account).
    device_id = entry.data.get(CONF_API_DEVICE_ID) or entry.data[CONF_DEVICE_ID]

    # Fresh start (e.g. after switching accounts): forget an old warning.
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))

    @callback
    def _session_taken() -> None:
        """The app logged in with this account and kicked HA out (and v.v.)."""
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SESSION_TAKEN,
            translation_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    client.on_session_taken = _session_taken

    log_seconds = entry.options.get(CONF_LOG_SECONDS, DEFAULT_LOG_SECONDS)
    status_minutes = entry.options.get(CONF_STATUS_MINUTES, DEFAULT_STATUS_MINUTES)

    status = SolityStatusCoordinator(hass, entry, client, device_id, status_minutes)
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


async def _async_update_listener(hass: HomeAssistant, entry: SolityConfigEntry) -> None:
    """Reload the entry when options (poll intervals) change."""
    await hass.config_entries.async_reload(entry.entry_id)
