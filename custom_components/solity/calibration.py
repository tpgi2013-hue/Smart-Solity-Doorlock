"""Per-home calibration of the post-open deadbolt verify schedule.

Every home relocks at a different pace (door closer speed, auto-lock delay,
how long people hold the door). While calibrating, each open is measured with
back-to-back status polls; after CALIBRATION_SAMPLES entries the verify
schedule is derived from the measured relock times and measuring stops.
Samples and the result are persisted, so a restart does not lose progress.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CALIBRATION_ALGO,
    CALIBRATION_MIN_OPEN_BASIS,
    CALIBRATION_SAMPLES,
    DOMAIN,
    VERIFY_LAST_SECONDS,
    VERIFY_MIN_FIRST_SECONDS,
)

STORAGE_VERSION = 1

_LOGGER = logging.getLogger(__name__)

BASIS_OPEN = "open"  # seconds after the lock's own logged open time
BASIS_DETECT = "detect"  # seconds after HA detected the open in the log


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.calibration.{entry_id}"


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1) of a non-empty list."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _ceil_half(value: float) -> float:
    return math.ceil(value * 2) / 2


def build_schedule(first: float, second: float) -> tuple[float, ...]:
    """first, second, then doubling up to VERIFY_LAST_SECONDS (increasing)."""
    first = max(VERIFY_MIN_FIRST_SECONDS, round(first, 1))
    second = max(round(second, 1), first + 1)
    out = [first, second]
    t = second
    while t < VERIFY_LAST_SECONDS:
        t = min(t * 2, VERIFY_LAST_SECONDS)
        out.append(round(t, 1))
    return tuple(out)


class SolityCalibration:
    """Collects relock samples and turns them into a verify schedule."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry_id)
        )
        self.samples: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        if data and data.get("algo") != CALIBRATION_ALGO:
            # Measured with an older method (e.g. v1.6.0-1.6.1 counted 15s
            # request hangs as relock time): start over.
            _LOGGER.info(
                "Solity calibration from method %s discarded; re-measuring",
                data.get("algo", 1),
            )
            await self.async_reset()
            return
        self.samples = list(data.get("samples") or [])
        self.result = data.get("result")

    @property
    def complete(self) -> bool:
        return self.result is not None

    @property
    def progress(self) -> str:
        return f"{min(len(self.samples), CALIBRATION_SAMPLES)}/{CALIBRATION_SAMPLES}"

    def add_sample(self, after_open: float | None, after_detect: float) -> bool:
        """Record one measured relock. Returns True when this completes it.

        ``after_open``: relock upper bound in seconds after the lock's logged
        open (None if the lock clock could not be trusted for this entry).
        ``after_detect``: the same, measured from HA's detection.
        """
        if self.complete:
            return False
        self.samples.append(
            {
                "after_open": after_open,
                "after_detect": after_detect,
                "at": dt_util.utcnow().isoformat(),
            }
        )
        if len(self.samples) >= CALIBRATION_SAMPLES:
            self.result = self._compute()
        self._save()
        return self.complete

    def _compute(self) -> dict[str, Any]:
        opens = [s["after_open"] for s in self.samples if s.get("after_open") is not None]
        if len(opens) >= CALIBRATION_MIN_OPEN_BASIS:
            basis, values = BASIS_OPEN, opens
        else:
            basis = BASIS_DETECT
            values = [s["after_detect"] for s in self.samples]
        p50 = percentile(values, 0.50)
        p75 = percentile(values, 0.75)
        p95 = percentile(values, 0.95)
        schedule = build_schedule(_ceil_half(p75), _ceil_half(p95))
        return {
            "basis": basis,
            "schedule": list(schedule),
            "first_verify_s": schedule[0],
            "p50_s": round(p50, 1),
            "p75_s": round(p75, 1),
            "p95_s": round(p95, 1),
            "samples": len(values),
            "calibrated_at": dt_util.utcnow().isoformat(),
            "algo": CALIBRATION_ALGO,
        }

    async def async_reset(self) -> None:
        self.samples = []
        self.result = None
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {"algo": CALIBRATION_ALGO, "samples": self.samples, "result": self.result}

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 1)
