"""Deadbolt verification: lock state must follow the real motor, not a timer."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.solity.api import SolityError
from custom_components.solity.const import DOMAIN

LOCK = "lock.hyeongwan"
EVENT = "event.hyeongwan_door_event"


def _log(dt, code="1"):
    return {"logDateTime": dt, "logCode": code, "logMessage": "열림", "mediaType": "8"}


class Fake:
    def __init__(self):
        self.deadbolt = 1
        self.fail = False
        self.status_calls = 0
        self.logs = [_log("2026-10-12 06:00:00")]

    async def get_status(self, _id):
        self.status_calls += 1
        if self.fail:
            raise SolityError("timeout")
        return {"deadBolt": self.deadbolt, "subLatch": self.deadbolt, "battery": 83}

    async def retrieve_log(self, _id, length=20):
        return list(self.logs)


async def _setup(hass, fake):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "a", "hashed_pwd": "b", "device_id": "D", "nickname": "현관"},
        options={"log_seconds": 10, "status_minutes": 30},
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


async def _tick(hass, sec):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=sec))
    await hass.async_block_till_done()


async def test_door_held_open_then_locks(hass, freezer):
    fake = Fake()
    _, p = await _setup(hass, fake)
    assert hass.states.get(LOCK).state == "locked"
    events = []

    def _on_change(e):
        new = e.data.get("new_state")
        if e.data["entity_id"] == EVENT and new and "event_type" in new.attributes:
            events.append(new.attributes["event_type"])

    hass.bus.async_listen("state_changed", _on_change)

    # someone opens from inside and holds the door
    fake.deadbolt = 0
    fake.logs.insert(0, _log("2026-10-12 07:05:22"))
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    assert hass.states.get(LOCK).state == "unlocked"
    assert events == ["open"]

    # 3s, 8s, 15s, 30s polls: still held open -> must stay unlocked (old bug: locked)
    for _ in range(3):
        freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
        assert hass.states.get(LOCK).state == "unlocked"
    assert "close" not in events

    # door closes, motor locks -> next verify poll (60s) confirms
    fake.deadbolt = 1
    freezer.tick(timedelta(seconds=35)); await _tick(hass, 0)
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["confirmed"] is True
    assert st.attributes["last_verify_seconds"] == 60
    assert st.attributes["last_verify_polls"] == 5
    assert events == ["open", "close"]
    calls = fake.status_calls
    # verify burst stopped
    freezer.tick(timedelta(seconds=600)); await _tick(hass, 0)
    assert fake.status_calls == calls
    p.stop()


async def test_missed_poll_does_not_revert_to_stale_locked(hass, freezer):
    fake = Fake()
    _, p = await _setup(hass, fake)
    fake.deadbolt = 0
    fake.fail = True  # lock asleep / cloud timeout during verify
    fake.logs.insert(0, _log("2026-10-12 07:05:22"))
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    st = hass.states.get(LOCK)
    assert st.state == "unlocked" and st.attributes["confirmed"] is False
    p.stop()


async def test_quick_entry_confirms_on_first_poll(hass, freezer):
    fake = Fake()
    _, p = await _setup(hass, fake)
    fake.logs.insert(0, _log("2026-10-12 07:10:00"))  # opened and already relocked
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    before = fake.status_calls
    freezer.tick(timedelta(seconds=6)); await _tick(hass, 0)
    st = hass.states.get(LOCK)
    assert st.state == "locked"
    assert fake.status_calls - before == 1
    assert st.attributes["last_verify_seconds"] == 3
    assert st.attributes["last_verify_polls"] == 1
    assert st.attributes["last_verify_result"] == "locked"
    assert st.attributes["verifying"] is False
    p.stop()


async def test_second_open_restarts_single_verify(hass, freezer):
    fake = Fake()
    _, p = await _setup(hass, fake)
    fake.deadbolt = 0
    fake.logs.insert(0, _log("2026-10-12 07:20:00"))
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    assert hass.states.get(LOCK).attributes["verifying"] is True
    # another open while the first burst is still running
    fake.logs.insert(0, _log("2026-10-12 07:20:12"))
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    st = hass.states.get(LOCK)
    assert st.state == "unlocked" and st.attributes["verifying"] is True
    # exactly one burst keeps running: lock -> confirmed once, then quiet
    fake.deadbolt = 1
    freezer.tick(timedelta(seconds=4)); await _tick(hass, 0)
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["verifying"] is False
    calls = fake.status_calls
    freezer.tick(timedelta(seconds=900)); await _tick(hass, 0)
    assert fake.status_calls == calls
    p.stop()


class SlowFake(Fake):
    """get_status takes ~0.8s (cloud -> module -> lock round trip)."""

    slow = False

    async def get_status(self, _id):
        import asyncio

        if self.slow:
            await asyncio.sleep(0.8)
        return await super().get_status(_id)


async def _run(hass, freezer, seconds, step=0.25):
    for _ in range(int(seconds / step)):
        freezer.tick(timedelta(seconds=step))
        await _tick(hass, 0)


async def test_measure_mode_times_relock_from_real_open(hass, freezer):
    fake = SlowFake()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "a", "hashed_pwd": "b", "device_id": "D", "nickname": "현관"},
        options={"log_seconds": 10, "status_minutes": 30, "measure_mode": True},
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
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake.slow = True
    base = fake.status_calls  # setup's first refresh
    assert hass.states.get(LOCK).attributes["measure_mode"] is True

    # the lock logged the open 4s before HA's log poll picks it up
    fake.deadbolt = 0
    now_kst = dt_util.now(dt_util.get_time_zone("Asia/Seoul"))
    open_at = (now_kst + timedelta(seconds=6)).strftime("%Y-%m-%d %H:%M:%S")
    fake.logs.insert(0, _log(open_at))
    await _run(hass, freezer, 10)  # log poll fires -> detection
    # door held ~3s more, then the motor locks
    await _run(hass, freezer, 3)
    fake.deadbolt = 1
    await _run(hass, freezer, 3)

    m = hass.states.get(LOCK).attributes["last_measurement"]
    assert m["result"] == "locked"
    assert m["polls"] >= 3 and fake.status_calls - base == m["polls"]
    # (the test clock fires timers on 0.25s ticks, so RTT reads ~0.5 here)
    assert 0 < m["rtt_avg_s"] <= 1.0
    assert m["first_poll_already_locked"] is False
    # lock window brackets the real relock and is about two round trips wide
    assert m["locked_after_open_min_s"] < m["locked_after_open_max_s"]
    assert m["locked_after_open_max_s"] - m["locked_after_open_min_s"] <= 2.5
    assert 3 <= m["detected_after_open_s"] <= 5  # log poll lag vs lock clock
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["verifying"] is False
    # no further wake-ups after confirmation
    calls = fake.status_calls
    await _run(hass, freezer, 60, step=5)
    assert fake.status_calls == calls
    p.stop()


async def test_measure_mode_off_by_default_uses_schedule(hass, freezer):
    fake = Fake()
    _, p = await _setup(hass, fake)
    assert hass.states.get(LOCK).attributes["last_measurement"] is None
    fake.logs.insert(0, _log("2026-10-12 07:30:00"))
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    before = fake.status_calls
    freezer.tick(timedelta(seconds=6)); await _tick(hass, 0)
    assert fake.status_calls - before == 1
    assert hass.states.get(LOCK).attributes["last_measurement"] is None
    p.stop()
