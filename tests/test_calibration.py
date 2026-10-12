"""Auto-calibration: measure 10 entries, derive this home's verify schedule."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.solity.calibration import (
    build_schedule,
    percentile,
    storage_key,
)
from custom_components.solity.const import DOMAIN

LOCK = "lock.hyeongwan"
CAL = "sensor.hyeongwan_lock_check_calibration"
ENTRY_ID = "E1"
KST = dt_util.get_time_zone("Asia/Seoul")
START = "2026-10-12 07:00:00+09:00"


class Fake:
    def __init__(self):
        self.deadbolt = 1
        self.status_calls = 0
        self.hang_next = 0  # this many upcoming calls hang (like the real cloud)
        self.logs = [{"logDateTime": "2026-10-12 06:00:00", "logCode": "1"}]

    async def get_status(self, _id):
        self.status_calls += 1
        if self.hang_next:
            self.hang_next -= 1
            await asyncio.sleep(15)  # cut short by the fast-poll timeout
        return {"deadBolt": self.deadbolt, "battery": 83}

    async def retrieve_log(self, _id, length=20):
        return list(self.logs)


# NOTE: the HA test harness fires asyncio timers up to ~0.5s early relative to
# the frozen wall clock, so "not yet" checks stay a full second before targets.
async def _tick(hass, freezer, seconds, step=0.25):
    for _ in range(round(seconds / step)):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()


async def _setup(hass, fake, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        data={"email": "a", "hashed_pwd": "b", "device_id": "D", "nickname": "현관"},
        # log poll effectively manual (600s) so tests choose the detection lag
        options={"log_seconds": 600, "status_minutes": 30, **options},
    )
    entry.add_to_hass(hass)
    p = patch.multiple(
        "custom_components.solity.api.SolityClient",
        get_status=lambda self, i: fake.get_status(i),
        retrieve_log=lambda self, i, length=20: fake.retrieve_log(i),
        open=AsyncMock(),
        close=AsyncMock(),
    )
    p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, p


async def _open(hass, freezer, entry, fake, detect_lag, open_offset=0.0):
    """Lock logs an open now (+open_offset on its clock); HA detects later."""
    fake.deadbolt = 0
    stamp = dt_util.now(KST) + timedelta(seconds=open_offset)
    fake.logs.insert(
        0, {"logDateTime": stamp.strftime("%Y-%m-%d %H:%M:%S"), "logCode": "1"}
    )
    await _tick(hass, freezer, detect_lag)
    await entry.runtime_data.log.async_refresh()
    await hass.async_block_till_done()


def _stored(samples=10, schedule=(6.0, 12.0, 24.0, 48.0), basis="open", algo=2):
    return {
        "version": 1,
        "minor_version": 1,
        "key": storage_key(ENTRY_ID),
        "data": {
            "algo": algo,
            "samples": [{"after_open": 5.0, "after_detect": 2.0}] * samples,
            "result": {
                "basis": basis,
                "schedule": list(schedule),
                "first_verify_s": schedule[0],
                "p50_s": 5.0,
                "p75_s": 5.6,
                "p95_s": 11.0,
                "samples": samples,
                "calibrated_at": "2026-10-11T00:00:00+00:00",
            },
        },
    }


def test_percentile_and_schedule():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([7], 0.95) == 7
    s = build_schedule(5.5, 11.0)
    assert s[:3] == (5.5, 11.0, 22.0) and s[-1] == 900
    assert all(a < b for a, b in zip(s, s[1:]))
    # second never collapses onto the first
    assert build_schedule(6, 6)[:2] == (6, 7)


async def test_calibrates_after_ten_entries(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    with patch(
        "custom_components.solity.coordinator.persistent_notification.async_create"
    ) as notify:
        entry, p = await _setup(hass, fake)
        assert hass.states.get(CAL).state == "calibrating"
        assert hass.states.get(LOCK).attributes["calibrating"] is True

        relocks = [4, 5, 5, 6, 6, 6, 7, 7, 8, 12]  # seconds after the real open
        for i, relock in enumerate(relocks):
            await _open(hass, freezer, entry, fake, detect_lag=3)
            await _tick(hass, freezer, relock - 3)
            fake.deadbolt = 1
            await _tick(hass, freezer, 2)
            m = hass.states.get(LOCK).attributes["last_measurement"]
            assert m["result"] == "locked" and m["lock_clock_ok"] is True
            # upper bound brackets the real relock within one poll interval
            assert relock <= m["locked_after_open_max_s"] <= relock + 1.0
            assert m["calibration"] == f"{i + 1}/10"
            await _tick(hass, freezer, 60, step=5)

        st = hass.states.get(CAL)
        assert st.state == "calibrated"
        assert st.attributes["basis"] == "open"
        true_p75 = percentile(relocks, 0.75)
        assert true_p75 <= st.attributes["first_verify_s"] <= true_p75 + 1.5
        assert hass.states.get(LOCK).attributes["calibrating"] is False
        notify.assert_called_once()

        # measuring stopped: next entry uses the schedule (one wake-up)
        before = fake.status_calls
        await _open(hass, freezer, entry, fake, detect_lag=3)
        fake.deadbolt = 1  # quick entry
        await _tick(hass, freezer, 30, step=0.5)
        assert fake.status_calls - before == 1
        assert hass.states.get(LOCK).state == "locked"
        p.stop()


async def test_schedule_anchored_to_real_open(hass, freezer, hass_storage):
    freezer.move_to(START)
    hass_storage[storage_key(ENTRY_ID)] = _stored(schedule=(6.0, 12.0, 24.0))
    fake = Fake()
    entry, p = await _setup(hass, fake)
    assert hass.states.get(CAL).state == "calibrated"

    await _open(hass, freezer, entry, fake, detect_lag=4)  # detected at open+4
    fake.deadbolt = 1
    before = fake.status_calls
    await _tick(hass, freezer, 1.0)  # open+5: not yet
    assert fake.status_calls == before
    await _tick(hass, freezer, 1.5)  # open+6.5: polled at open+6
    assert fake.status_calls == before + 1
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["last_verify_seconds"] == 6.0
    p.stop()


async def test_late_detection_polls_immediately_once(hass, freezer, hass_storage):
    freezer.move_to(START)
    hass_storage[storage_key(ENTRY_ID)] = _stored(schedule=(6.0, 12.0, 24.0))
    fake = Fake()
    entry, p = await _setup(hass, fake)
    before = fake.status_calls
    await _open(hass, freezer, entry, fake, detect_lag=9)  # 6s point already past
    await _tick(hass, freezer, 0.25)
    assert fake.status_calls == before + 1  # one catch-up poll, not a burst
    assert hass.states.get(LOCK).state == "unlocked"
    fake.deadbolt = 1
    await _tick(hass, freezer, 3)  # open+12
    assert fake.status_calls == before + 2
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


async def test_manual_first_overrides(hass, freezer, hass_storage):
    freezer.move_to(START)
    hass_storage[storage_key(ENTRY_ID)] = _stored(schedule=(6.0, 12.0, 24.0))
    fake = Fake()
    entry, p = await _setup(hass, fake, first_verify_seconds=3)
    assert hass.states.get(CAL).state == "manual"
    assert hass.states.get(CAL).attributes["first_verify_s"] == 3
    await _open(hass, freezer, entry, fake, detect_lag=1)
    fake.deadbolt = 1
    before = fake.status_calls
    await _tick(hass, freezer, 2.5)  # open+3.5
    assert fake.status_calls == before + 1
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


async def test_lock_clock_ahead_falls_back_to_detection(hass, freezer, hass_storage):
    freezer.move_to(START)
    hass_storage[storage_key(ENTRY_ID)] = _stored(schedule=(6.0, 12.0, 24.0))
    fake = Fake()
    entry, p = await _setup(hass, fake)
    # lock clock 30s fast: logged open looks like it is in the future
    await _open(hass, freezer, entry, fake, detect_lag=2, open_offset=30)
    fake.deadbolt = 1
    before = fake.status_calls
    await _tick(hass, freezer, 5.0)  # detect+5 (open+6 would be long past)
    assert fake.status_calls == before
    await _tick(hass, freezer, 1.5)  # detect+6.5 -> polled at detect+6
    assert fake.status_calls == before + 1
    p.stop()


async def test_untrusted_clock_samples_use_detection_basis(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    for _ in range(10):  # lock clock 40s ahead on every entry
        await _open(hass, freezer, entry, fake, detect_lag=2, open_offset=40)
        await _tick(hass, freezer, 3)
        fake.deadbolt = 1
        await _tick(hass, freezer, 2)
        assert hass.states.get(LOCK).attributes["last_measurement"]["lock_clock_ok"] is False
        await _tick(hass, freezer, 30, step=5)
    st = hass.states.get(CAL)
    assert st.state == "calibrated" and st.attributes["basis"] == "detect"
    assert 3 <= st.attributes["first_verify_s"] <= 4.5
    p.stop()


async def test_progress_survives_reload(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    for _ in range(3):
        await _open(hass, freezer, entry, fake, detect_lag=2)
        await _tick(hass, freezer, 2)
        fake.deadbolt = 1
        await _tick(hass, freezer, 2)
        await _tick(hass, freezer, 30, step=5)
    assert hass.states.get(CAL).attributes["samples_collected"] == 3
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    st = hass.states.get(CAL)
    assert st.state == "calibrating" and st.attributes["samples_collected"] == 3
    p.stop()


async def test_recalibrate_from_options(hass, freezer, hass_storage):
    freezer.move_to(START)
    hass_storage[storage_key(ENTRY_ID)] = _stored()
    fake = Fake()
    entry, p = await _setup(hass, fake)
    assert hass.states.get(CAL).state == "calibrated"
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "log_seconds": 600,
            "status_minutes": 30,
            "auto_calibrate": True,
            "recalibrate": True,
            "first_verify_seconds": 0,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert "recalibrate" not in entry.options  # one-shot, never stored
    st = hass.states.get(CAL)
    assert st.state == "calibrating" and st.attributes["samples_collected"] == 0
    p.stop()


@pytest.mark.parametrize("auto", [False])
async def test_auto_calibrate_off_uses_default(hass, freezer, auto):
    freezer.move_to(START)
    fake = Fake()
    _, p = await _setup(hass, fake, auto_calibrate=auto)
    st = hass.states.get(CAL)
    assert st.state == "default" and st.attributes["schedule_s"][0] == 3
    assert hass.states.get(LOCK).attributes["calibrating"] is False
    p.stop()


async def test_hung_first_poll_is_cut_and_retried(hass, freezer, hass_storage):
    """Real-world: the first get_status after an open hangs ~15s."""
    freezer.move_to(START)
    hass_storage[storage_key(ENTRY_ID)] = _stored(schedule=(6.0, 12.0, 24.0))
    fake = Fake()
    entry, p = await _setup(hass, fake)
    await _open(hass, freezer, entry, fake, detect_lag=4)
    fake.deadbolt = 1
    fake.hang_next = 1
    await _tick(hass, freezer, 6)  # open+10: hung poll at +6 cut at ~+8.5, retried
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["last_verify_polls"] == 2
    # the hang must not flip the lock to unavailable
    assert st.state != "unavailable"
    p.stop()


async def test_measurement_with_hang_stays_precise(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    await _open(hass, freezer, entry, fake, detect_lag=4)
    fake.hang_next = 1  # first measurement poll hangs (as observed)
    await _tick(hass, freezer, 3)
    fake.deadbolt = 1
    await _tick(hass, freezer, 3)
    m = hass.states.get(LOCK).attributes["last_measurement"]
    assert m["result"] == "locked" and m["failures"] == 1
    assert m["blind_s"] <= 3 and m["sample_used"] is True
    # v1.6.0 would have reported ~20s here (15s hang + detection lag)
    assert m["locked_after_open_max_s"] <= 9
    assert hass.states.get(CAL).attributes["samples_collected"] == 1
    p.stop()


async def test_long_blind_gap_is_not_a_sample(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    await _open(hass, freezer, entry, fake, detect_lag=4)
    fake.hang_next = 3  # ~7.5s of failed polls right before the lock reading
    fake.deadbolt = 1
    await _tick(hass, freezer, 12)
    m = hass.states.get(LOCK).attributes["last_measurement"]
    assert m["result"] == "locked" and m["blind_s"] > 5
    assert m["sample_used"] is False
    assert hass.states.get(CAL).attributes["samples_collected"] == 0
    assert hass.states.get(LOCK).state == "locked"  # still reported correctly
    p.stop()


async def test_old_method_calibration_is_discarded(hass, freezer, hass_storage):
    freezer.move_to(START)
    old = _stored(schedule=(25.5, 28.5, 57.0))
    del old["data"]["algo"]  # stored by v1.6.0/1.6.1
    hass_storage[storage_key(ENTRY_ID)] = old
    fake = Fake()
    _, p = await _setup(hass, fake)
    st = hass.states.get(CAL)
    assert st.state == "calibrating" and st.attributes["samples_collected"] == 0
    assert st.attributes["schedule_s"][0] == 3  # default until re-measured
    p.stop()
