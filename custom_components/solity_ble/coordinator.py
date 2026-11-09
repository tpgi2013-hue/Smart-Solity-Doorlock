"""Status + usage-log coordinator for Solity BLE.

Each poll reads the lock's status (battery, deadbolt, credential counts) and
its usage log (last open: method, slot, time) in a single BLE connection.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import SolityBleClient, SolityBleError, SolityPollData

_LOGGER = logging.getLogger(__name__)


class SolityBleCoordinator(DataUpdateCoordinator[SolityPollData]):
    """Polls lock status + log over BLE and accepts data pushed from commands."""

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

    async def _async_update_data(self) -> SolityPollData:
        try:
            data = await self.client.async_poll()
        except SolityBleError as err:
            raise UpdateFailed(str(err)) from err
        # Keep the last known values for anything this poll didn't return.
        prev = self.data
        if prev is not None:
            if data.status is None:
                data.status = prev.status
            if data.last_open is None:
                data.last_open = prev.last_open
        return data

    @callback
    def apply_poll(self, data: SolityPollData | None) -> None:
        """Merge data captured during an open/close command."""
        if data is None:
            return
        prev = self.data
        if prev is not None:
            if data.status is None:
                data.status = prev.status
            if data.last_open is None:
                data.last_open = prev.last_open
        self.async_set_updated_data(data)
