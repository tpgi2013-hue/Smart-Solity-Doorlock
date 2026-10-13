"""Lock state follows the real deadbolt; post-open checks are paced by the door."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.solity.api import SolityError
from custom_components.solity.const import DOMAIN

LOCK = "lock.hyeongwan"
DOOR = "binary_sensor.hyeongwan_door"
ENTRY_ID = "E1"
START = "2026-10-12 07:00:00+09:00"


def _log(dt, code="1"):
    return {"logDateTime": dt, "logCode": code, "logMessage": "열림", "mediaType": "8"}


class Fake:
    """Cloud stand-in: deadbolt (1 = locked) and door (subLatch 1 = closed)."""

    def __init__(self):
        self.deadbolt = 1
        self.door_closed = 1
        self.fail = False
        self.hang_next = 0  # this many upcoming calls hang (like the real cloud)
        self.status_calls = 0
        self.ids: set[str] = set()  # device ids the integration asked for
        self.logs = [_log("2026-10-12 06:00:00")]

    async def get_status(self, _id):
        self.status_calls += 1
        self.ids.add(_id)
        if self.hang_next:
            self.hang_next -= 1
            await asyncio.sleep(15)  # cut short by the fast-poll timeout
        if self.fail:
            raise SolityError("timeout")
        return {"deadBolt": self.deadbolt, "subLatch": self.door_closed, "battery": 83}

    async def retrieve_log(self, _id, length=20):
        self.ids.add(_id)
        return list(self.logs)

    def opens(self, stamp="2026-10-12 07:05:22"):
        """Someone unlocks and opens the door."""
        self.deadbolt = 0
        self.door_closed = 0
        self.logs.insert(0, _log(stamp))


async def _setup(hass, fake, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        data={"email": "a", "hashed_pwd": "b", "device_id": "D", "nickname": "현관"},
        # log poll effectively manual (600s): tests trigger detection themselves
        options=options or {"log_seconds": 600, "status_minutes": 30},
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


# NOTE: the HA test harness fires asyncio timers up to ~0.5s early relative to
# the frozen wall clock, so "not yet" checks stay well before their targets.
async def _tick(hass, freezer, seconds, step=0.25):
    for _ in range(round(seconds / step)):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()


async def _detect(hass, entry):
    await entry.runtime_data.log.async_refresh()
    await hass.async_block_till_done()


def _results(hass):
    return [x["result"] for x in hass.states.get(LOCK).attributes["last_verify_trace"]]


async def test_quick_entry_one_poll(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.logs.insert(0, _log("2026-10-12 07:00:00"))  # opened, already relocked
    before = fake.status_calls
    await _detect(hass, entry)
    await _tick(hass, freezer, 1)
    st = hass.states.get(LOCK)
    assert st.state == "locked" and fake.status_calls - before == 1
    assert st.attributes["last_verify_result"] == "locked"
    assert hass.states.get(DOOR).state == "off"  # closed
    p.stop()


async def test_door_held_open_then_closes_and_locks(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)

    fake.opens()
    await _detect(hass, entry)
    await _tick(hass, freezer, 0.5)
    assert hass.states.get(LOCK).state == "unlocked"
    assert hass.states.get(DOOR).state == "on"  # open

    # door stays open: rechecks back off 5s -> 10s (not every second)
    calls = fake.status_calls
    await _tick(hass, freezer, 14)
    assert fake.status_calls - calls == 2  # at ~5s and ~15s
    assert hass.states.get(LOCK).state == "unlocked"

    # door shuts; auto-lock follows a moment later
    fake.door_closed = 1
    await _tick(hass, freezer, 20)  # next check (~+20s from start of backoff)
    assert _results(hass)[-1] == "closed_unlocked"
    assert hass.states.get(DOOR).state == "off"
    fake.deadbolt = 1
    await _tick(hass, freezer, 2)  # closed -> rechecked after 1.5s
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["confirmed"] is True
    assert _results(hass)[-2:] == ["closed_unlocked", "locked"]
    calls = fake.status_calls
    await _tick(hass, freezer, 600, step=10)
    assert fake.status_calls == calls  # burst stopped
    p.stop()


async def test_closed_but_never_locks_backs_off(hass, freezer):
    """Manual-lock mode: door closed, bolt never thrown -> don't hammer it."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.opens()
    fake.door_closed = 1  # unlocked but door shut, auto-lock off
    await _detect(hass, entry)
    before = fake.status_calls
    await _tick(hass, freezer, 6)
    # 3 quick 1.5s rechecks, then backoff
    assert 3 <= fake.status_calls - before <= 4
    await _tick(hass, freezer, 900, step=5)
    st = hass.states.get(LOCK)
    assert st.state == "unlocked"
    assert st.attributes["last_verify_result"] == "still_unlocked"
    assert fake.status_calls - before <= 15  # whole 15-min window
    p.stop()


async def test_hung_first_poll_is_cut_and_retried(hass, freezer):
    """Real-world: the first get_status after an open hangs ~15s."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.logs.insert(0, _log("2026-10-12 07:00:00"))
    fake.hang_next = 1
    await _detect(hass, entry)
    await _tick(hass, freezer, 4)  # cut at 2.5s, retried
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["last_verify_polls"] == 2
    assert st.attributes["last_verify_seconds"] <= 4
    p.stop()


async def test_missed_polls_do_not_revert_to_stale_locked(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.opens()
    fake.fail = True  # lock asleep / cloud errors during the checks
    await _detect(hass, entry)
    await _tick(hass, freezer, 10)
    st = hass.states.get(LOCK)
    assert st.state == "unlocked" and st.attributes["confirmed"] is False
    assert st.state != "unavailable"
    assert hass.states.get(DOOR).state == "unknown"  # not read back yet
    assert "no_answer" in _results(hass)
    p.stop()


async def test_second_open_restarts_single_burst(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.opens("2026-10-12 07:20:00")
    await _detect(hass, entry)
    await _tick(hass, freezer, 2)
    assert hass.states.get(LOCK).attributes["verifying"] is True
    fake.opens("2026-10-12 07:20:12")  # another open mid-burst
    await _detect(hass, entry)
    await _tick(hass, freezer, 1)
    fake.door_closed = 1
    fake.deadbolt = 1
    await _tick(hass, freezer, 6)
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["verifying"] is False
    calls = fake.status_calls
    await _tick(hass, freezer, 900, step=10)
    assert fake.status_calls == calls  # exactly one burst was running
    p.stop()


async def test_upgrade_cleans_up_calibration_leftovers(hass, freezer, hass_storage):
    freezer.move_to(START)
    key = f"{DOMAIN}.calibration.{ENTRY_ID}"
    hass_storage[key] = {"version": 1, "key": key, "data": {"algo": 2, "samples": []}}
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create("sensor", DOMAIN, "D_calibration")
    ent_reg.async_get_or_create("event", DOMAIN, "D_door_event")
    fake = Fake()
    entry, p = await _setup(
        hass,
        fake,
        options={
            "log_seconds": 600,
            "status_minutes": 30,
            "auto_calibrate": True,
            "first_verify_seconds": 0,
            "measure_mode": False,
        },
    )
    assert dict(entry.options) == {"log_seconds": 600, "status_minutes": 30}
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, "D_calibration") is None
    assert ent_reg.async_get_entity_id("event", DOMAIN, "D_door_event") is None
    await hass.async_block_till_done()
    assert key not in hass_storage
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


async def test_options_flow_only_poll_intervals(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert set(flow["data_schema"].schema) == {"log_seconds", "status_minutes"}
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"log_seconds": 10, "status_minutes": 30}
    )
    assert result["type"] == "create_entry"
    p.stop()
