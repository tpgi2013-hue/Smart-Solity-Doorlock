"""Data update coordinators for Smart Solity."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SolityAuthError, SolityClient, SolityError
from .const import (
    DOMAIN,
    LOCK_VERIFY_OFFSETS,
    LOG_UTC_OFFSET_HOURS,
    MEASURE_MAX_POLLS,
    MEASURE_MIN_INTERVAL,
    MEASURE_WINDOW_SECONDS,
    OPEN_LOG_CODES,
    TOLERATED_STATUS_FAILURES,
)

_LOGGER = logging.getLogger(__name__)
_LOG_TZ = timezone(timedelta(hours=LOG_UTC_OFFSET_HOURS))


def parse_log_dt(value: str | None) -> datetime | None:
    """Parse the lock's logDateTime ('YYYY-MM-DD HH:MM:SS', KST) to aware dt."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_LOG_TZ)
    except ValueError:
        return None


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 1)


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
        measure_mode: bool = False,
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
        # Outcome of the latest verify burst, for tuning LOCK_VERIFY_OFFSETS:
        # seconds after the open was detected, and how many polls it took.
        self.last_verify_seconds: int | None = None
        self.last_verify_polls: int | None = None
        self.last_verify_result: str | None = None  # locked / still_unlocked
        # Measurement mode: back-to-back polls after an open (see const).
        self.measure_mode = measure_mode
        self.last_measurement: dict[str, Any] | None = None

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
    def start_verify(self, open_at: datetime | None = None) -> None:
        """(Re)start the post-open burst of status polls.

        ``open_at`` is the lock's own timestamp of the open (from the log), used
        by measurement mode to time the relock from the real open.
        """
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = self.config_entry.async_create_background_task(
            self.hass, self._async_verify(open_at), f"{DOMAIN}_verify_lock"
        )
        # Make the lock entity's 'verifying' attribute visible right away.
        self.async_update_listeners()

    async def _async_verify(self, open_at: datetime | None = None) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        polls = 0
        measured = False
        try:
            if self.measure_mode:
                polls = await self._async_measure(start, open_at)
                if polls < 0:  # locked within the measurement window
                    return
                measured = True
            for offset in LOCK_VERIFY_OFFSETS:
                delay = start + offset - loop.time()
                if measured and delay <= 0:
                    continue  # the measurement window already covered this
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.async_refresh()
                polls += 1
                if self.last_poll_ok and self.is_locked:
                    self._record_verify(offset, polls, "locked")
                    return
            self._record_verify(LOCK_VERIFY_OFFSETS[-1], polls, "still_unlocked")
        finally:
            # A newer open may have replaced this task; don't clobber it.
            if self._verify_task is asyncio.current_task():
                self._verify_task = None
                self.async_update_listeners()

    async def _async_measure(self, start: float, open_at: datetime | None) -> int:
        """Back-to-back polls; returns -1 if locked, else the polls spent.

        The deadbolt is sampled somewhere inside each poll's round trip, so the
        relock happened after the START of the last poll that still read
        unlocked and before the END of the first poll that read locked.
        """
        loop = asyncio.get_running_loop()
        detected_at = dt_util.utcnow()
        polls = failures = 0
        rtts: list[float] = []
        last_unlocked_start: datetime | None = None
        locked_end: datetime | None = None

        while polls < MEASURE_MAX_POLLS and loop.time() - start < MEASURE_WINDOW_SECONDS:
            t0 = loop.time()
            w0 = dt_util.utcnow()
            await self.async_refresh()
            t1 = loop.time()
            polls += 1
            if self.last_poll_ok:
                rtts.append(t1 - t0)
                if self.is_locked:
                    locked_end = dt_util.utcnow()
                    break
                last_unlocked_start = w0
            else:
                failures += 1
            wait = MEASURE_MIN_INTERVAL - (loop.time() - t0)
            if wait > 0:
                await asyncio.sleep(wait)

        ref = open_at or detected_at
        lo = last_unlocked_start or ref  # nothing seen unlocked -> since open
        result = "locked" if locked_end else "still_unlocked"
        self.last_measurement = {
            "open_at": open_at.isoformat() if open_at else None,
            "detected_after_open_s": _secs(open_at, detected_at),
            "locked_after_open_min_s": _secs(ref, lo) if locked_end else None,
            "locked_after_open_max_s": _secs(ref, locked_end),
            "locked_after_detect_max_s": _secs(detected_at, locked_end),
            "first_poll_already_locked": bool(locked_end) and last_unlocked_start is None,
            "result": result,
            "polls": polls,
            "failures": failures,
            "rtt_avg_s": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "rtt_max_s": round(max(rtts), 2) if rtts else None,
        }
        _LOGGER.info("Solity lock measurement: %s", self.last_measurement)
        self.async_update_listeners()
        if locked_end:
            self._record_verify(
                round(loop.time() - start), polls, "locked"
            )
            return -1
        return polls

    @callback
    def _record_verify(self, seconds: int, polls: int, result: str) -> None:
        self.last_verify_seconds = seconds
        self.last_verify_polls = polls
        self.last_verify_result = result
        _LOGGER.info(
            "Solity deadbolt verify: %s after %ss (%s polls)", result, seconds, polls
        )

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
            opens = [x for x in new if x.get("logCode") in OPEN_LOG_CODES]
            if opens:
                self.mark_unlocked()
                # newest-first list -> opens[0] is the latest open
                self.start_verify(parse_log_dt(opens[0].get("logDateTime")))

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
