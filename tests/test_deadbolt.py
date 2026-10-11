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
    hass.bus.async_listen("state_changed", lambda e: e.data["entity_id"] == EVENT and events.append(e.data["new_state"].attributes["event_type"]))

    # someone opens from inside and holds the door
    fake.deadbolt = 0
    fake.logs.insert(0, _log("2026-10-12 07:05:22"))
    freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
    assert hass.states.get(LOCK).state == "unlocked"
    assert events == ["open"]

    # 5s, 10s, 20s polls: still held open -> must stay unlocked (old bug: locked)
    for _ in range(3):
        freezer.tick(timedelta(seconds=10)); await _tick(hass, 0)
        assert hass.states.get(LOCK).state == "unlocked"
    assert "close" not in events

    # door closes, motor locks -> next verify poll confirms
    fake.deadbolt = 1
    freezer.tick(timedelta(seconds=25)); await _tick(hass, 0)
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["confirmed"] is True
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
    assert hass.states.get(LOCK).state == "locked"
    assert fake.status_calls - before == 1
    p.stop()
