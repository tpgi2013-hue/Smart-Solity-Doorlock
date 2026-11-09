"""Lock platform for Smart Solity.

The lock state is the REAL deadbolt (motor) state from ``get_status`` — never
a timer guess. The status coordinator invalidates it to 'unlocked' as soon as
an open appears in the event log (or is sent from HA) and then re-polls the
lock, paced by the door state, until the deadbolt reads locked again.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolityConfigEntry
from .api import SolityError
from .ble import SolityBleClient, SolityBleError
from .const import CONF_DEVICE_ID, CONF_NICKNAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import SolityStatusCoordinator

_LOGGER = logging.getLogger(__name__)


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
        self._ble: SolityBleClient | None = entry.runtime_data.ble
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=entry.data.get(CONF_NICKNAME) or "Solity Doorlock",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        """Availability.

        With the direct-BLE path configured the lock can still be opened over
        BLE when the cloud status poll is failing (a stuck WiFi+ module, the
        very case BLE exists for). Keep the entity available for commands then,
        even though the deadbolt *state* below may be unknown/stale. Without
        BLE, fall back to the cloud coordinator's availability.
        """
        if self._ble is not None:
            return True
        return super().available

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
            # True while the lock doesn't answer at all and is re-polled slowly.
            "not_answering": self.coordinator.recovering,
            # False = value is assumed (open seen / poll missed), not read back.
            "confirmed": self.coordinator.confirmed,
            # Latest post-open check: seconds after the open was detected, how
            # many status polls it took, and what each poll saw.
            "last_verify_seconds": self.coordinator.last_verify_seconds,
            "last_verify_polls": self.coordinator.last_verify_polls,
            "last_verify_result": self.coordinator.last_verify_result,
            "last_verify_trace": self.coordinator.last_verify_trace,
            # Latest open/close API call: which, and how long it took (s).
            "last_command": self.coordinator.last_command_action,
            "last_command_seconds": self.coordinator.last_command_seconds,
        }

    async def _command(self, action: str) -> None:
        client = self.coordinator.client
        send = client.close if action == "lock" else client.open
        try:
            async with self.coordinator.command():
                started = time.monotonic()
                try:
                    await send(self.coordinator.device_id)
                finally:
                    self.coordinator.record_command(
                        action, time.monotonic() - started
                    )
        except SolityError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"{action}_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _unlock_command(self) -> None:
        """Open the lock: try direct BLE first (fast, local), cloud as fallback.

        BLE and the cloud open share one command() guard so a BLE failure that
        falls through to the cloud does not re-enter it.
        """
        async with self.coordinator.command():
            if self._ble is not None:
                started = time.monotonic()
                try:
                    await self._ble.async_open()
                    self.coordinator.record_command(
                        "unlock(ble)", time.monotonic() - started
                    )
                    return
                except SolityBleError as err:
                    self.coordinator.record_command(
                        "unlock(ble-fail)", time.monotonic() - started
                    )
                    _LOGGER.warning(
                        "Solity BLE open failed (%s); falling back to cloud", err
                    )

            client = self.coordinator.client
            started = time.monotonic()
            try:
                await client.open(self.coordinator.device_id)
            except SolityError as err:
                self.coordinator.record_command(
                    "unlock(cloud-fail)", time.monotonic() - started
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="unlock_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            self.coordinator.record_command(
                "unlock(cloud)", time.monotonic() - started
            )

    async def async_lock(self, **kwargs: Any) -> None:
        await self._command("lock")
        self.coordinator.cancel_unlock_grace()
        self.coordinator.start_verify()

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._unlock_command()
        self.coordinator.mark_unlocked(from_ha=True)
        self.coordinator.start_verify(after_ha_unlock=True)

    async def async_open(self, **kwargs: Any) -> None:
        await self.async_unlock(**kwargs)
