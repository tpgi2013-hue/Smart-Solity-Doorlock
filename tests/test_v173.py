"""v1.7.3: remote opens (mediaType 7) are not assumed to open the door."""
from .test_deadbolt import DOOR, LOCK, START, Fake, _detect, _setup, _tick


async def test_remote_open_in_log_is_not_assumed_open(hass, freezer):
    """Someone opened remotely from the app: don't guess the door is open."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.logs.insert(
        0, {"logDateTime": "2026-10-12 07:00:00", "logCode": "1", "mediaType": "7"}
    )
    fake.hang_next = 1
    await _detect(hass, entry)
    assert hass.states.get(LOCK).state == "unlocked"
    assert hass.states.get(DOOR).state == "unknown"
    assert hass.states.get(LOCK).attributes["verifying"] is True
    p.stop()


async def test_ha_unlock_then_its_log_entry_keeps_real_door(hass, freezer):
    """HA unlock reads the real door; its own log entry must not undo that."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.deadbolt = 0  # unlocked, door still shut
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    await _tick(hass, freezer, 1)
    assert hass.states.get(DOOR).state == "off"
    fake.logs.insert(
        0, {"logDateTime": "2026-10-12 07:00:05", "logCode": "1", "mediaType": "7"}
    )
    fake.hang_next = 1  # a restarted check would leave the door unknown here
    await _detect(hass, entry)
    st = hass.states.get(DOOR)
    assert st.state == "off" and st.attributes["confirmed"] is True
    p.stop()
