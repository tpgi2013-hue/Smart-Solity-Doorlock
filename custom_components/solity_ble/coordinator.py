"""Status coordinator for Solity BLE.

Periodically reads the lock's status over BLE (battery, deadbolt, credential
counts, system mode). Each open/close also yields a fresh status frame, which
the lock entity pushes in here so the sensors update immediately without an
extra connection.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import SolityBleClient, SolityBleError, SolityStatus
from .const import UNLOCKED_RECHECK_MAX, UNLOCKED_RECHECK_SECONDS

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
        self._recheck_cancel: CALLBACK_TYPE | None = None
        self._rechecks = 0

    async def _async_update_data(self) -> SolityStatus | None:
        try:
            status = await self.client.async_read_status()
        except SolityBleError as err:
            raise UpdateFailed(str(err)) from err
        if status is None:
            # Connected/authed but no status frame arrived; keep last data.
            return self.data
        self._schedule_recheck(status)
        return status

    @callback
    def apply_status(self, status: SolityStatus | None) -> None:
        """Push a status captured during an open/close command."""
        if status is not None:
            self.async_set_updated_data(status)

    @callback
    def _schedule_recheck(self, status: SolityStatus) -> None:
        """Re-read soon while the deadbolt reads retracted.

        A read taken while someone is using the door says 'unlocked'; without
        this the stale value would stay until the next regular poll.
        """
        self.cancel_recheck()
        if status.is_locked is not False:
            self._rechecks = 0
            return
        if self._rechecks >= UNLOCKED_RECHECK_MAX:
            return
        self._rechecks += 1

        @callback
        def _fire(_now) -> None:
            self._recheck_cancel = None
            self.hass.async_create_task(self.async_request_refresh())

        self._recheck_cancel = async_call_later(
            self.hass, UNLOCKED_RECHECK_SECONDS, _fire
        )

    @callback
    def cancel_recheck(self) -> None:
        if self._recheck_cancel is not None:
            self._recheck_cancel()
            self._recheck_cancel = None
