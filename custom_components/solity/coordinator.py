"""Data update coordinators for Smart Solity."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SolityAuthError, SolityClient, SolityError
from .const import (
    DOMAIN,
    LOCK_VERIFY_OFFSETS,
    OPEN_LOG_CODES,
    TOLERATED_STATUS_FAILURES,
)

_LOGGER = logging.getLogger(__name__)


class SolityStatusCoordinator(DataUpdateCoordinator[dict]):
    """Polls door-lock status (battery, deadbolt). Wakes the lock, so run slow.

    The deadbolt value here is the ONLY real lock (motor) state the cloud
    exposes. After an open is seen in the event log (or sent from HA) the
    cached deadbolt is invalidated to 'unlocked' and a short burst of status
    polls confirms when the motor has actually thrown the bolt again.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SolityClient,
        device_id: str,
        status_minutes: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(minutes=status_minutes),
        )
        self.config_entry = entry
        self.client = client
        self.device_id = device_id
        self._failures = 0
        # True only when the latest data came fresh from the lock (not a
        # kept-over value after a missed poll).
        self.last_poll_ok = False
        self._verify_task: asyncio.Task | None = None
        self._last_log_dt: str | None = None

    # ------------------------------------------------------------------ poll
    async def _async_update_data(self) -> dict:
        try:
            data = await self.client.get_status(self.device_id)
        except SolityAuthError as err:
            self.last_poll_ok = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolityError as err:
            # A sleeping lock / cloud timeout misses a poll. Don't flap the
            # entities to 'unavailable' on the first miss — keep last values.
            self.last_poll_ok = False
            self._failures += 1
            if self.data and self._failures <= TOLERATED_STATUS_FAILURES:
                _LOGGER.debug(
                    "Solity status miss %s/%s, keeping last data: %s",
                    self._failures,
                    TOLERATED_STATUS_FAILURES,
                    err,
                )
                return self.data
            raise UpdateFailed(str(err)) from err

        if not data:
            # HTTP ok but the lock returned no payload (asleep). Same policy.
            self.last_poll_ok = False
            self._failures += 1
            if self.data and self._failures <= TOLERATED_STATUS_FAILURES:
                _LOGGER.debug(
                    "Solity status empty %s/%s, keeping last data",
                    self._failures,
                    TOLERATED_STATUS_FAILURES,
                )
                return self.data
            return data

        self._failures = 0
        self.last_poll_ok = True
        return data

    # ------------------------------------------------------------- deadbolt
    @property
    def is_locked(self) -> bool | None:
        """Real deadbolt state from the last status poll (1 = locked)."""
        value = (self.data or {}).get("deadBolt")
        if value is None:
            return None
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return None

    @property
    def verifying(self) -> bool:
        return self._verify_task is not None and not self._verify_task.done()

    @callback
    def mark_unlocked(self) -> None:
        """An open happened: drop the cached 'locked' so it can't come back.

        Without this, a missed verify poll would fall back to the pre-open
        deadbolt=1 and show 'locked' while the door is actually open.
        """
        data = dict(self.data or {})
        data["deadBolt"] = 0
        self.last_poll_ok = False
        self.async_set_updated_data(data)

    @callback
    def start_verify(self) -> None:
        """(Re)start the post-open burst of status polls."""
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = self.config_entry.async_create_background_task(
            self.hass, self._async_verify(), f"{DOMAIN}_verify_lock"
        )
        # Make the lock entity's 'verifying' attribute visible right away.
        self.async_update_listeners()

    async def _async_verify(self) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            for offset in LOCK_VERIFY_OFFSETS:
                delay = start + offset - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.async_refresh()
                if self.last_poll_ok and self.is_locked:
                    _LOGGER.debug("Solity deadbolt locked after %ss", offset)
                    return
            _LOGGER.debug(
                "Solity deadbolt still unlocked after %ss; back to normal polling",
                LOCK_VERIFY_OFFSETS[-1],
            )
        finally:
            self._verify_task = None
            self.async_update_listeners()

    @callback
    def attach_log(self, log: SolityLogCoordinator) -> CALLBACK_TYPE:
        """Watch the fast event log; on a new open, invalidate + verify."""
        logs = log.data or []
        self._last_log_dt = logs[0].get("logDateTime") if logs else None

        @callback
        def _on_log() -> None:
            logs = log.data or []
            if not logs:
                return
            newest = logs[0].get("logDateTime") or ""
            if self._last_log_dt is None:
                self._last_log_dt = newest
                return
            if newest <= self._last_log_dt:
                return
            new = [x for x in logs if (x.get("logDateTime") or "") > self._last_log_dt]
            self._last_log_dt = newest
            if any(x.get("logCode") in OPEN_LOG_CODES for x in new):
                self.mark_unlocked()
                self.start_verify()

        return log.async_add_listener(_on_log)


class SolityLogCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the access log (cloud-stored). Cheap — run fast for live events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SolityClient,
        device_id: str,
        log_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_log",
            update_interval=timedelta(seconds=log_seconds),
        )
        self.config_entry = entry
        self.client = client
        self.device_id = device_id

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self.client.retrieve_log(self.device_id, length=20)
        except SolityAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolityError as err:
            raise UpdateFailed(str(err)) from err
