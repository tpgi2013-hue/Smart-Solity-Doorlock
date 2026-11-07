"""v1.7.6: solity.wake (guarded pre-wake), last_command_seconds, solity_unlock."""
import asyncio

from homeassistant.core import callback

from custom_components.solity.const import DOMAIN, EVENT_UNLOCK

from .test_deadbolt import LOCK, START, Fake, _detect, _log, _setup, _tick


async def _wake(hass):
    return await hass.services.async_call(
        DOMAIN, "wake", {}, blocking=True, return_response=True
    )


async def test_wake_sends_one_poll_and_is_rate_limited(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    before = fake.status_calls
    assert await _wake(hass) == {"E1": "started"}
    await hass.async_block_till_done()
    assert fake.status_calls == before + 1
    await _tick(hass, freezer, 3)
    assert (await _wake(hass)) == {"E1": "skipped_recent"}
    assert fake.status_calls == before + 1
    await _tick(hass, freezer, 9)
    assert (await _wake(hass)) == {"E1": "started"}
    await hass.async_block_till_done()
    assert fake.status_calls == before + 2
    p.stop()


async def test_wake_skipped_during_command_and_cancelled_by_it(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    # a wake poll that hangs: an unlock arriving meanwhile cancels it at once
    fake.hang_next = 1
    assert (await _wake(hass)) == {"E1": "started"}
    await _tick(hass, freezer, 0.5)
    assert status._wake_task is not None and not status._wake_task.done()
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert status._wake_task is None
    assert entry.runtime_data.status.client.open.await_count == 1
    # while a command runs, no wake goes out
    async with status.command():
        status._last_wake = None
        assert status.async_wake() == "skipped_command"
    p.stop()


async def test_wake_skipped_while_verifying(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    fake.door_closed = 0
    fake.deadbolt = 0
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert status.verifying
    before = fake.status_calls
    assert (await _wake(hass)) == {"E1": "skipped_verifying"}
    assert fake.status_calls == before
    p.stop()


async def test_last_command_seconds_attribute(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    assert hass.states.get(LOCK).attributes["last_command_seconds"] is None
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    st = hass.states.get(LOCK)
    assert st.attributes["last_command"] == "unlock"
    assert isinstance(st.attributes["last_command_seconds"], float)
    p.stop()


async def test_unlock_event_for_new_open_log(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    events = []

    @callback
    def _on(event):
        events.append(event.data)

    hass.bus.async_listen(EVENT_UNLOCK, _on)
    fake.logs.insert(0, {**_log("2026-10-12 07:00:05", "7"), "mediaType": "7"})
    fake.logs.insert(0, _log("2026-10-12 07:00:09"))  # inside handle / other
    await _detect(hass, entry)
    await hass.async_block_till_done()
    assert [e["time"] for e in events] == ["2026-10-12 07:00:05", "2026-10-12 07:00:09"]
    assert events[0]["remote"] is True and events[1]["remote"] is False
    assert events[0]["device_id"] == "D" and events[0]["entry_id"] == "E1"
    # an old/unchanged log fires nothing
    events.clear()
    await _detect(hass, entry)
    await hass.async_block_till_done()
    assert events == []
    p.stop()
