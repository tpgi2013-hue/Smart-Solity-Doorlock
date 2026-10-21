"""Data update coordinators for Smart Solity."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SolityAuthError, SolityClient, SolityError
from .const import (
    DOMAIN,
    LOG_MEDIA_REMOTE,
    OPEN_LOG_CODES,
    RECOVER_DELAYS,
    TOLERATED_STATUS_FAILURES,
    UNLOCK_GRACE,
    VERIFY_BACKOFF_MAX,
    VERIFY_CLOSED_FAST_TRIES,
    VERIFY_CLOSED_RECHECK,
    VERIFY_MAX_POLLS,
    VERIFY_OPEN_FIRST,
    VERIFY_POLL_RETRIES,
    VERIFY_POLL_TIMEOUT,
    VERIFY_TRACE_LEN,
    VERIFY_UNKNOWN_RECHECK,
    VERIFY_WINDOW,
)

_LOGGER = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SolityStatusCoordinator(DataUpdateCoordinator[dict]):
    """Polls door-lock status (battery, deadbolt, door). Wakes the lock: run slow.

    The cloud never pushes a lock/close event, so the real state is read with
    get_status: ``deadBolt`` (1 = bolt thrown = locked) and ``subLatch`` (the
    push-pull mortise's door-detect latch: 1 = door closed, 0 = door open).
    After an open is seen in the event log (or sent from HA) the cached state
    is invalidated and a short, door-aware burst of polls confirms the relock.
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
        # Slow re-polls while the lock doesn't answer at all.
        self._recover_task: asyncio.Task | None = None
        self._last_log_dt: str | None = None
        # Outcome of the latest post-open burst (diagnostics).
        self.last_verify_seconds: float | None = None
        self.last_verify_polls: int | None = None
        self.last_verify_result: str | None = None  # locked / still_unlocked
        self.last_verify_trace: list[dict[str, Any]] = []
        # True while a post-open poll runs: short timeout, retried by caller.
        self._fast = False
        # True while the door state is assumed 'open' after an open in the
        # log, until the lock is read back (or doesn't answer).
        self._door_assumed = False
        # Loop time until which a 'locked' reading after an HA unlock is stale.
        self._grace_until: float | None = None
        # True when the latest reading was such a stale 'locked' (shown as
        # unlocked; the check reads the lock again shortly).
        self._stale_locked = False

    # ------------------------------------------------------------------ poll
    async def _async_update_data(self) -> dict:
        try:
            if self._fast:
                async with asyncio.timeout(VERIFY_POLL_TIMEOUT):
                    data = await self.client.get_status(self.device_id)
            else:
                data = await self.client.get_status(self.device_id)
        except SolityAuthError as err:
            self.last_poll_ok = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SolityError, TimeoutError) as err:
            self.last_poll_ok = False
            if self._fast and self.data:
                # Post-open poll gave up early; the caller retries right away,
                # so this must not count toward going 'unavailable'.
                _LOGGER.debug("Solity fast status poll missed: %r", err)
                return self.data
            # A sleeping lock / cloud timeout misses a poll. Don't flap the
            # entities to 'unavailable' on the first miss — keep last values.
            self._failures += 1
            self._schedule_recover()
            if self.data and self._failures <= TOLERATED_STATUS_FAILURES:
                _LOGGER.debug(
                    "Solity status miss %s/%s, keeping last data: %s",
                    self._failures,
                    TOLERATED_STATUS_FAILURES,
                    err,
                )
                return self.data
            raise UpdateFailed(str(err)) from err

        if not isinstance(data, dict) or _as_int(data.get("deadBolt")) is None:
            # HTTP ok but no usable status ({} from a sleeping lock, or a
            # payload without the bolt). Same policy as a miss, and never
            # replace good data with it.
            self.last_poll_ok = False
            if self._fast and self.data:
                return self.data
            self._failures += 1
            self._schedule_recover()
            if self.data and self._failures <= TOLERATED_STATUS_FAILURES:
                _LOGGER.debug(
                    "Solity status empty %s/%s, keeping last data: %r",
                    self._failures,
                    TOLERATED_STATUS_FAILURES,
                    data,
                )
                return self.data
            raise UpdateFailed("the lock returned no status")

        self._failures = 0
        self.last_poll_ok = True
        self._door_assumed = False
        self._stale_locked = False
        if self._grace_until is not None:
            if _as_int(data.get("deadBolt")) == 1 and (
                asyncio.get_running_loop().time() < self._grace_until
            ):
                # Read before the unlock took effect: not a relock.
                self._stale_locked = True
                data = {**data, "deadBolt": 0}
            else:
                self._grace_until = None
        return data

    async def _async_fast_refresh(self) -> None:
        """One post-open status poll with the short timeout."""
        self._fast = True
        try:
            await self.async_refresh()
        finally:
            self._fast = False

    # ------------------------------------------------------------ lock/door
    @property
    def is_locked(self) -> bool | None:
        """Real deadbolt state from the last status poll (1 = locked)."""
        value = _as_int((self.data or {}).get("deadBolt"))
        return None if value is None else value == 1

    @property
    def door_open(self) -> bool | None:
        """Door state from the door-detect latch (subLatch 0 = open)."""
        value = _as_int((self.data or {}).get("subLatch"))
        return None if value is None else value == 0

    @property
    def confirmed(self) -> bool:
        """The shown lock state was read back from the lock as is."""
        return self.last_poll_ok and not self._stale_locked

    @property
    def verifying(self) -> bool:
        return self._verify_task is not None and not self._verify_task.done()

    @property
    def recovering(self) -> bool:
        return self._recover_task is not None and not self._recover_task.done()

    @callback
    def mark_unlocked(self, assume_open: bool = False, from_ha: bool = False) -> None:
        """An open happened: drop the cached 'locked' so it can't come back.

        ``assume_open`` (an open from the lock's own log: fingerprint, card,
        code, inside handle ...) shows the door as open right away; the first
        status check corrects it. Otherwise (a remote unlock from HA, which
        need not open the door) the door is unknown until read back.
        ``from_ha``: HA just sent the unlock, so a 'locked' reading in the
        next UNLOCK_GRACE seconds is stale, not a relock.
        """
        if from_ha:
            self._grace_until = asyncio.get_running_loop().time() + UNLOCK_GRACE
        data = dict(self.data or {})
        data["deadBolt"] = 0
        if assume_open:
            data["subLatch"] = 0
        else:
            data.pop("subLatch", None)
        self._door_assumed = assume_open
        self.last_poll_ok = False
        self.async_set_updated_data(data)

    @callback
    def cancel_unlock_grace(self) -> None:
        """HA sent a lock command: a 'locked' reading is real again."""
        self._grace_until = None
        self._stale_locked = False

    @callback
    def _drop_assumed_door(self) -> None:
        """The lock didn't answer: don't keep showing a guessed 'open'."""
        if self._door_assumed and self.data:
            self._door_assumed = False
            data = dict(self.data)
            data.pop("subLatch", None)
            self.data = data

    # ---------------------------------------------------------------- verify
    @callback
    def start_verify(self) -> None:
        """(Re)start the post-open burst of status polls."""
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        if self.recovering:
            # The verify burst polls faster; it reschedules recovery if needed.
            self._recover_task.cancel()
            self._recover_task = None
        self._verify_task = self.config_entry.async_create_background_task(
            self.hass, self._async_verify(), f"{DOMAIN}_verify_lock"
        )
        # Make the lock entity's 'verifying' attribute visible right away.
        self.async_update_listeners()

    async def _async_verify(self) -> None:
        """Poll until the deadbolt reads locked, pacing by the door state."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        polls = 0
        open_wait = VERIFY_OPEN_FIRST
        closed_streak = 0
        trace: list[dict[str, Any]] = []
        self.last_verify_trace = trace
        try:
            while polls < VERIFY_MAX_POLLS:
                # A missed (timed-out) poll is retried immediately.
                for _ in range(1 + VERIFY_POLL_RETRIES):
                    await self._async_fast_refresh()
                    polls += 1
                    if self.last_poll_ok:
                        break
                elapsed = round(loop.time() - start, 1)

                if not self.last_poll_ok:
                    trace.append({"t": elapsed, "result": "no_answer"})
                    self._drop_assumed_door()
                    wait = VERIFY_UNKNOWN_RECHECK
                elif self._stale_locked:
                    # Bolt still reads out right after HA's unlock: the
                    # command hasn't landed yet. Read again shortly.
                    trace.append({"t": elapsed, "result": "locked_early"})
                    wait = VERIFY_CLOSED_RECHECK
                elif self.is_locked:
                    trace.append({"t": elapsed, "result": "locked"})
                    self._record_verify(elapsed, polls, "locked", trace)
                    return
                elif self.door_open is False:
                    # Door closed but bolt not out yet: auto-lock is about to
                    # fire. If it doesn't after a few quick checks the lock is
                    # likely in manual-lock mode -> stop hammering it.
                    trace.append({"t": elapsed, "result": "closed_unlocked"})
                    closed_streak += 1
                    if closed_streak <= VERIFY_CLOSED_FAST_TRIES:
                        wait = VERIFY_CLOSED_RECHECK
                    else:
                        wait = open_wait
                        open_wait = min(open_wait * 2, VERIFY_BACKOFF_MAX)
                elif self.door_open is True:
                    trace.append({"t": elapsed, "result": "open"})
                    closed_streak = 0
                    wait = open_wait
                    open_wait = min(open_wait * 2, VERIFY_BACKOFF_MAX)
                else:
                    trace.append({"t": elapsed, "result": "unlocked"})
                    wait = VERIFY_UNKNOWN_RECHECK

                del trace[:-VERIFY_TRACE_LEN]
                self.async_update_listeners()
                if loop.time() - start + wait > VERIFY_WINDOW:
                    break
                await asyncio.sleep(wait)
            answered = any(x["result"] != "no_answer" for x in trace)
            self._record_verify(
                round(loop.time() - start, 1),
                polls,
                "still_unlocked" if answered else "no_answer",
                trace,
            )
        finally:
            # A newer open may have replaced this task; don't clobber it.
            if self._verify_task is asyncio.current_task():
                self._verify_task = None
                if not self.last_poll_ok:
                    self._schedule_recover()
                self.async_update_listeners()

    # --------------------------------------------------------------- recover
    @callback
    def _schedule_recover(self) -> None:
        """Lock not answering: re-poll slowly until it does (not every hour)."""
        if self.recovering or self.verifying or self.data is None:
            # No data yet = still setting up; HA's setup retry covers that.
            return
        self._recover_task = self.config_entry.async_create_background_task(
            self.hass, self._async_recover(), f"{DOMAIN}_recover_lock"
        )

    async def _async_recover(self) -> None:
        attempt = 0
        _LOGGER.warning(
            "Solity lock is not answering status requests; retrying every %s-%ss "
            "until it does (if it lasts, restart the lock: take a battery out)",
            RECOVER_DELAYS[0],
            RECOVER_DELAYS[-1],
        )
        while True:
            await asyncio.sleep(RECOVER_DELAYS[min(attempt, len(RECOVER_DELAYS) - 1)])
            attempt += 1
            await self.async_refresh()
            if self.last_poll_ok:
                break
        _LOGGER.info("Solity lock answers again (after %s retries)", attempt)
        if self._recover_task is asyncio.current_task():
            self._recover_task = None
        if self.is_locked is False:
            # Back after an outage and not locked: watch it relock as usual.
            self.start_verify()
        else:
            self.async_update_listeners()

    @callback
    def _record_verify(
        self, seconds: float, polls: int, result: str, trace: list[dict[str, Any]]
    ) -> None:
        self.last_verify_seconds = seconds
        self.last_verify_polls = polls
        self.last_verify_result = result
        self.last_verify_trace = trace[-VERIFY_TRACE_LEN:]
        _LOGGER.info(
            "Solity deadbolt verify: %s after %ss (%s polls) %s",
            result,
            seconds,
            polls,
            self.last_verify_trace,
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
            if not opens:
                return
            if all(str(x.get("mediaType")) == LOG_MEDIA_REMOTE for x in opens):
                # Remote open (app / HA): nobody need be at the door. When HA
                # sent it, the check is already running and has read the real
                # door; don't overwrite that with a guess.
                if not self.verifying:
                    self.mark_unlocked()
                    self.start_verify()
                return
            self.mark_unlocked(assume_open=True)
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
