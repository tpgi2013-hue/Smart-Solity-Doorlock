"""v1.7.4: reconfigure without async_update_reload_and_abort, unlock grace,
empty status payloads never replace good data."""
from unittest.mock import AsyncMock, patch

from .test_deadbolt import DOOR, LOCK, START, Fake, _results, _setup, _tick
from .test_v171 import _reconfigure


async def test_reconfigure_reloads_through_update_listener(hass, freezer):
    """Changed data: the entry's own listener reloads it (no reload_and_abort)."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await _reconfigure(hass, entry, [{"myDeviceId": "M1"}])
        await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert entry.data["api_device_id"] == "M1"
    assert entry.data["device_id"] == "D"
    assert reload.call_count == 1
    assert "M1" in fake.ids
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


async def test_reconfigure_same_account_still_reloads_once(hass, freezer):
    """Nothing changed (same login, same lock): reload anyway, exactly once."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "email": "ha@example.com",
            "hashed_pwd": _hash("pw"),
            "api_device_id": None,
        },
    )
    await hass.async_block_till_done()
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload, patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=hass.config_entries.async_schedule_reload,
    ) as schedule:
        result = await _reconfigure(hass, entry, [{"myDeviceId": "D"}])
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert schedule.call_count == 1
    assert reload.call_count <= 1
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


def _hash(pw):
    from custom_components.solity.api import hash_password

    return hash_password(pw)


async def test_stale_locked_right_after_ha_unlock_is_ignored(hass, freezer):
    """Bolt still reads 'out' just after HA's unlock: not a relock."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    # fake.deadbolt stays 1: the unlock hasn't landed on the first reads
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    await _tick(hass, freezer, 0.5)
    st = hass.states.get(LOCK)
    assert st.state == "unlocked"
    assert st.attributes["verifying"] is True
    assert st.attributes["confirmed"] is False
    assert _results(hass)[0] == "locked_early"
    # now the motor moves
    fake.deadbolt = 0
    await _tick(hass, freezer, 2)
    st = hass.states.get(LOCK)
    assert st.state == "unlocked" and st.attributes["confirmed"] is True
    # ...and the door shuts and auto-lock fires: a real relock is accepted
    fake.deadbolt = 1
    await _tick(hass, freezer, 3)
    st = hass.states.get(LOCK)
    assert st.state == "locked"
    assert st.attributes["last_verify_result"] == "locked"
    assert "locked_early" in _results(hass)
    p.stop()


async def test_locked_after_grace_is_real(hass, freezer):
    """The unlock never landed: after the grace the lock shows locked again."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    await _tick(hass, freezer, 0.5)
    assert hass.states.get(LOCK).state == "unlocked"
    await _tick(hass, freezer, 8)
    st = hass.states.get(LOCK)
    assert st.state == "locked"
    assert st.attributes["confirmed"] is True
    assert st.attributes["verifying"] is False
    p.stop()


async def test_lock_command_cancels_grace(hass, freezer):
    """HA unlock then HA lock right away: the 'locked' read is taken at once."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.deadbolt = 1  # unlock not landed yet: grace is active
    await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    await _tick(hass, freezer, 0.5)
    assert hass.states.get(LOCK).state == "unlocked"
    await hass.services.async_call("lock", "lock", {"entity_id": LOCK}, blocking=True)
    await _tick(hass, freezer, 0.5)
    st = hass.states.get(LOCK)
    assert st.state == "locked"
    assert st.attributes["last_verify_result"] == "locked"
    p.stop()


async def test_logged_open_has_no_grace(hass, freezer):
    """A fingerprint open that relocked already: 'locked' is believed."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.logs.insert(
        0, {"logDateTime": "2026-10-12 07:05:22", "logCode": "1", "mediaType": "8"}
    )
    await entry.runtime_data.log.async_refresh()
    await _tick(hass, freezer, 0.5)
    st = hass.states.get(LOCK)
    assert st.state == "locked"
    assert _results(hass) == ["locked"]
    p.stop()


async def test_empty_status_never_replaces_good_data(hass, freezer):
    """{} from a sleeping lock keeps the last values, then goes unavailable."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    with patch.object(fake, "get_status", AsyncMock(return_value={})):
        for _ in range(3):
            await status.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(LOCK).state == "locked"
            assert hass.states.get(DOOR).state == "off"
            assert status.data["deadBolt"] == 1
        await status.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "unavailable"
    assert status.data["deadBolt"] == 1  # never overwritten with {}
    # the lock answers again
    await status.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


async def test_payload_without_bolt_is_a_miss(hass, freezer):
    """A status without deadBolt is treated like no answer (no 'unknown')."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    status = entry.runtime_data.status
    with patch.object(fake, "get_status", AsyncMock(return_value={"battery": 80})):
        await status.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "locked"
    assert status.last_poll_ok is False
    p.stop()


async def test_get_status_non_dict_payload():
    """controlDeviceMessage that isn't a JSON object -> {}."""
    from custom_components.solity.api import SolityClient

    client = SolityClient(None, "a", "b")
    for msg in ('"busy"', "[1, 2]", "null", "{bad", None):
        with patch.object(
            client,
            "control",
            AsyncMock(return_value={"contents": {"controlDeviceMessage": msg}}),
        ):
            assert await client.get_status("D") == {}
    with patch.object(
        client,
        "control",
        AsyncMock(
            return_value={"contents": {"controlDeviceMessage": '{"deadBolt": 1}'}}
        ),
    ):
        assert await client.get_status("D") == {"deadBolt": 1}
