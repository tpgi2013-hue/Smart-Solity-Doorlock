"""v1.7.5: an HA unlock no longer floods the WiFi+ module with status polls.

Repeated HA unlocks (face recognition) used to fire up to four get_status
calls within ~10s of each open command, and the module stopped answering until
the lock was restarted. Opens detected in the log keep the immediate check.
"""
import asyncio
from unittest.mock import patch

from .test_deadbolt import LOCK, START, Fake, _detect, _log, _setup, _tick


async def test_ha_unlock_is_sent_at_once_and_checked_after_settle(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    open_mock = entry.runtime_data.status.client.open
    before = fake.status_calls
    fake.deadbolt = 0
    fake.door_closed = 0  # door held open: next check only 5s later
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert open_mock.await_count == 1  # the unlock itself is not delayed
    assert hass.states.get(LOCK).state == "unlocked"
    await _tick(hass, freezer, 4)
    assert fake.status_calls == before  # no poll while the module is busy
    await _tick(hass, freezer, 1.5)
    assert fake.status_calls == before + 1
    st = hass.states.get(LOCK)
    assert st.state == "unlocked" and st.attributes["confirmed"] is True
    p.stop()


async def test_ha_unlock_missed_check_retried_gently(hass, freezer):
    """A hung check after an HA unlock: one retry 3s later, not a burst."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    before = fake.status_calls
    fake.deadbolt = 0
    fake.door_closed = 0  # door held open: next check only 5s later
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    fake.hang_next = 1
    await _tick(hass, freezer, 5.5)  # first check started, hanging
    assert fake.status_calls == before + 1
    await _tick(hass, freezer, 3)  # cut at 2.5s, then waiting 3s
    assert fake.status_calls == before + 1  # no back-to-back retry
    await _tick(hass, freezer, 3)
    assert fake.status_calls == before + 2
    st = hass.states.get(LOCK)
    assert st.state == "unlocked" and st.attributes["confirmed"] is True
    p.stop()


async def test_logged_open_still_checked_at_once(hass, freezer):
    """Fingerprint / code / card opens: unchanged from v1.7.4."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.logs.insert(0, _log("2026-10-12 07:00:00"))
    fake.hang_next = 1
    before = fake.status_calls
    await _detect(hass, entry)
    await _tick(hass, freezer, 0.5)
    assert fake.status_calls == before + 1  # first poll right away
    await _tick(hass, freezer, 3.5)  # cut at 2.5s, retried at once
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["last_verify_polls"] == 2
    p.stop()


async def test_status_poll_waits_for_command(hass, freezer):
    """A status poll never goes out while an open/close is being sent."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    sending = asyncio.Event()
    release = asyncio.Event()

    async def slow_open(self, _id):
        sending.set()
        await release.wait()

    before = fake.status_calls
    with patch("custom_components.solity.api.SolityClient.open", slow_open):
        unlock = hass.async_create_task(
            hass.services.async_call(
                "lock", "unlock", {"entity_id": LOCK}, blocking=True
            )
        )
        await sending.wait()
        poll = hass.async_create_task(status.async_refresh())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert fake.status_calls == before  # held back
        release.set()
        await unlock
        await poll
    assert fake.status_calls == before + 1
    p.stop()


async def test_command_does_not_wait_for_status_poll(hass, freezer):
    """A face-recognition unlock goes out even while a poll is hanging."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    open_mock = status.client.open
    fake.hang_next = 1
    poll = hass.async_create_task(status.async_refresh())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert fake.status_calls >= 1
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert open_mock.await_count == 1
    poll.cancel()
    p.stop()


async def test_one_status_poll_in_flight(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    active = 0
    peak = 0

    async def counted(self, _id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        for _ in range(3):
            await asyncio.sleep(0)
        active -= 1
        return {"deadBolt": 1, "subLatch": 1, "battery": 83}

    with patch("custom_components.solity.api.SolityClient.get_status", counted):
        await asyncio.gather(*(status._get_status() for _ in range(4)))
    assert peak == 1
    p.stop()
