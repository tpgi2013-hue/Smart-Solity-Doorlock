"""Data update coordinators for Smart Solity."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SolityAuthError, SolityClient, SolityError
from .calibration import BASIS_DETECT, BASIS_OPEN, SolityCalibration, build_schedule
from .const import (
    CLOCK_AHEAD_TOLERANCE_SECONDS,
    DOMAIN,
    LOCK_VERIFY_OFFSETS,
    LOG_UTC_OFFSET_HOURS,
    MAX_DETECT_LAG_SECONDS,
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


def clock_ok(open_at: datetime | None, detected_at: datetime) -> bool:
    """True if the lock's logged open time is plausible against HA's clock."""
    if open_at is None:
        return False
    lag = (detected_at - open_at).total_seconds()
    return -CLOCK_AHEAD_TOLERANCE_SECONDS <= lag <= MAX_DETECT_LAG_SECONDS


SOURCE_CALIBRATING = "calibrating"
SOURCE_CALIBRATED = "calibrated"
SOURCE_MANUAL = "manual"
SOURCE_DEFAULT = "default"


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
        calibration: SolityCalibration,
        auto_calibrate: bool = True,
        manual_first: float = 0,
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
        self.last_verify_seconds: float | None = None
        self.last_verify_polls: int | None = None
        self.last_verify_result: str | None = None  # locked / still_unlocked
        # Per-home schedule: auto-calibration and/or a manual first poll.
        self.calibration = calibration
        self.auto_calibrate = auto_calibrate
        self.manual_first = float(manual_first or 0)
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

    # -------------------------------------------------------------- schedule
    @property
    def calibrating(self) -> bool:
        """Measuring entries (back-to-back polls) to calibrate this home."""
        return self.auto_calibrate and not self.calibration.complete

    @property
    def schedule_source(self) -> str:
        if self.manual_first > 0:
            return SOURCE_MANUAL
        if self.calibrating:
            return SOURCE_CALIBRATING
        if self.auto_calibrate and self.calibration.complete:
            return SOURCE_CALIBRATED
        return SOURCE_DEFAULT

    @property
    def verify_schedule(self) -> tuple[tuple[float, ...], str]:
        """(offsets in seconds, basis) used after an open."""
        source = self.schedule_source
        if source == SOURCE_MANUAL:
            first = self.manual_first
            return build_schedule(first, max(first * 1.5, first + 2)), BASIS_OPEN
        if source == SOURCE_CALIBRATED:
            result = self.calibration.result or {}
            return tuple(result["schedule"]), result.get("basis", BASIS_DETECT)
        return LOCK_VERIFY_OFFSETS, BASIS_DETECT

    # ---------------------------------------------------------------- verify
    @callback
    def start_verify(self, open_at: datetime | None = None) -> None:
        """(Re)start the post-open burst of status polls.

        ``open_at`` is the lock's own timestamp of the open (from the log): the
        calibrated schedule is anchored to it, so detection lag doesn't shift
        it, and calibration measures relock times from it.
        """
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = self.config_entry.async_create_background_task(
            self.hass, self._async_verify(open_at), f"{DOMAIN}_verify_lock"
        )
        # Make the lock entity's 'verifying' attribute visible right away.
        self.async_update_listeners()

    async def _async_verify(self, open_at: datetime | None = None) -> None:
        detected_at = dt_util.utcnow()
        polls = 0
        measured = False
        try:
            if self.calibrating:
                polls = await self._async_measure(detected_at, open_at)
                if polls < 0:  # locked within the measurement window
                    return
                measured = True

            offsets, basis = self.verify_schedule
            ref = detected_at
            if basis == BASIS_OPEN and clock_ok(open_at, detected_at):
                ref = open_at
            now = dt_util.utcnow()
            targets = [(o, ref + timedelta(seconds=o)) for o in offsets]
            pending = [t for t in targets if t[1] > now]
            overdue = [t for t in targets if t[1] <= now]
            # Points already past (detection was late, or measurement covered
            # them): one immediate catch-up poll instead of several, none at
            # all if the measurement window just polled continuously.
            seq = pending if measured or not overdue else [(overdue[-1][0], now), *pending]

            for offset, target in seq:
                delay = (target - dt_util.utcnow()).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.async_refresh()
                polls += 1
                if self.last_poll_ok and self.is_locked:
                    self._record_verify(offset, polls, "locked")
                    return
            self._record_verify(offsets[-1], polls, "still_unlocked")
        finally:
            # A newer open may have replaced this task; don't clobber it.
            if self._verify_task is asyncio.current_task():
                self._verify_task = None
                self.async_update_listeners()

    async def _async_measure(
        self, detected_at: datetime, open_at: datetime | None
    ) -> int:
        """Back-to-back polls; returns -1 if locked, else the polls spent.

        The deadbolt is sampled somewhere inside each poll's round trip, so the
        relock happened after the START of the last poll that still read
        unlocked and before the END of the first poll that read locked.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        polls = failures = 0
        rtts: list[float] = []
        last_unlocked_start: datetime | None = None
        locked_end: datetime | None = None
        trusted = clock_ok(open_at, detected_at)

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

        ref = open_at if trusted else detected_at
        lo = last_unlocked_start or ref  # nothing seen unlocked -> since open
        after_open_max = _secs(open_at, locked_end) if trusted else None
        after_detect_max = _secs(detected_at, locked_end)
        self.last_measurement = {
            "open_at": open_at.isoformat() if open_at else None,
            "lock_clock_ok": trusted,
            "detected_after_open_s": _secs(open_at, detected_at),
            "locked_after_open_min_s": _secs(ref, lo) if locked_end and trusted else None,
            "locked_after_open_max_s": after_open_max,
            "locked_after_detect_max_s": after_detect_max,
            "first_poll_already_locked": bool(locked_end) and last_unlocked_start is None,
            "result": "locked" if locked_end else "still_unlocked",
            "polls": polls,
            "failures": failures,
            "rtt_avg_s": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "rtt_max_s": round(max(rtts), 2) if rtts else None,
        }
        _LOGGER.info("Solity lock measurement: %s", self.last_measurement)

        # Only entries that relocked inside the window become samples; a door
        # held open past it would skew the stats.
        if locked_end:
            if self.calibration.add_sample(after_open_max, after_detect_max):
                self._notify_calibrated()
            self.last_measurement["calibration"] = self.calibration.progress
            self._record_verify(round(loop.time() - start, 1), polls, "locked")
            self.async_update_listeners()
            return -1
        self.last_measurement["calibration"] = self.calibration.progress
        self.async_update_listeners()
        return polls

    @callback
    def _notify_calibrated(self) -> None:
        result = self.calibration.result or {}
        basis = "실제 열림" if result.get("basis") == BASIS_OPEN else "HA 감지"
        _LOGGER.info("Solity calibration complete: %s", result)
        persistent_notification.async_create(
            self.hass,
            (
                f"출입 {result.get('samples')}회를 측정해 잠김 확인 스케줄을 맞췄습니다.\n\n"
                f"- 첫 확인: **{basis} 후 {result.get('first_verify_s')}초**\n"
                f"- 잠김 소요 시간: 중앙값 {result.get('p50_s')}초 · "
                f"75% {result.get('p75_s')}초 · 95% {result.get('p95_s')}초\n"
                f"- 스케줄: {', '.join(str(x) for x in result.get('schedule', [])[:5])} … 초\n\n"
                "측정(연속 조회)은 자동으로 꺼졌습니다. 다시 맞추려면 통합 옵션에서 "
                "'보정 다시 하기'를 선택하세요."
            ),
            title="솔리티 도어락 보정 완료",
            notification_id=f"{DOMAIN}_calibrated_{self.config_entry.entry_id}",
        )

    @callback
    def _record_verify(self, seconds: float, polls: int, result: str) -> None:
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
