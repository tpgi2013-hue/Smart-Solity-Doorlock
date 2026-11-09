"""Status coordinator for Solity BLE.

Periodically reads the lock's status over BLE (battery, deadbolt, credential
counts, system mode). Each open/close also yields a fresh status frame, which
the lock entity pushes in here so the sensors update immediately without an
extra connection.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import SolityBleClient, SolityBleError, SolityStatus

_LOGGER = logging.getLogger(__name__)


class SolityBleCoordinator(DataUpdateCoordinator[SolityStatus | None]):
    """Polls lock status over BLE and accepts status pushed from commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: SolityBleClient,
        name: str,
        poll_minutes: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(minutes=poll_minutes) if poll_minutes > 0 else None,
        )
        self.client = client

    async def _async_update_data(self) -> SolityStatus | None:
        try:
            status = await self.client.async_read_status()
        except SolityBleError as err:
            raise UpdateFailed(str(err)) from err
        if status is None:
            # Connected/authed but no status frame arrived; keep last data.
            return self.data
        return status

    @callback
    def apply_status(self, status: SolityStatus | None) -> None:
        """Push a status captured during an open/close command."""
        if status is not None:
            self.async_set_updated_data(status)
