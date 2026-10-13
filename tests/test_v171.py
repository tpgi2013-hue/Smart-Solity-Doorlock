"""v1.7.1: lock-not-answering recovery, account switch, session conflict, errors."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.solity.api import SolityClient, SolityError
from custom_components.solity.const import DOMAIN

from .test_deadbolt import DOOR, LOCK, START, Fake, _detect, _setup, _tick

LAST = "sensor.hyeongwan_last_unlock"


async def test_lock_not_answering_is_repolled_until_back(hass, freezer):
    """Field case: WiFi module stuck after an open, nothing answers for ages."""
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.opens()
    fake.fail = True
    await _detect(hass, entry)
    await _tick(hass, freezer, 60, step=1)  # whole burst: 40 missed polls
    st = hass.states.get(LOCK)
    assert st.attributes["verifying"] is False
    assert st.attributes["last_verify_result"] == "no_answer"
    assert st.attributes["not_answering"] is True
    assert hass.states.get(DOOR).state == "unknown"

    # still dead at the 1st retry (+60s), then the lock is rebooted
    await _tick(hass, freezer, 60, step=1)
    assert hass.states.get(LOCK).attributes["not_answering"] is True
    fake.fail = False
    fake.deadbolt = 1
    fake.door_closed = 1
    calls = fake.status_calls
    await _tick(hass, freezer, 121, step=1)  # 2nd retry (+120s) answers
    st = hass.states.get(LOCK)
    assert st.state == "locked" and st.attributes["confirmed"] is True
    assert st.attributes["not_answering"] is False
    assert hass.states.get(DOOR).state == "off"
    assert fake.status_calls - calls == 1
    await _tick(hass, freezer, 900, step=10)
    assert fake.status_calls - calls == 1  # recovery stopped
    p.stop()


async def test_recovered_unlocked_lock_is_watched_again(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.opens()
    fake.fail = True
    await _detect(hass, entry)
    await _tick(hass, freezer, 60, step=1)
    fake.fail = False  # back, but door open and unlocked
    await _tick(hass, freezer, 61, step=1)
    st = hass.states.get(LOCK)
    assert st.attributes["not_answering"] is False
    assert st.attributes["verifying"] is True  # normal door-aware checks
    fake.door_closed = 1
    fake.deadbolt = 1
    await _tick(hass, freezer, 30, step=1)
    assert hass.states.get(LOCK).state == "locked"
    p.stop()


async def test_regular_poll_miss_starts_recovery(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    fake.fail = True
    await entry.runtime_data.status.async_refresh()  # e.g. the hourly poll
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).attributes["not_answering"] is True
    fake.fail = False
    await _tick(hass, freezer, 61, step=1)
    assert hass.states.get(LOCK).attributes["not_answering"] is False
    p.stop()


async def test_lock_command_error_is_readable(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    with patch.object(
        SolityClient, "close", AsyncMock(side_effect=SolityError("no response"))
    ):
        with pytest.raises(HomeAssistantError) as exc:
            await hass.services.async_call(
                "lock", "lock", {"entity_id": LOCK}, blocking=True
            )
    assert exc.value.translation_key == "lock_failed"
    assert exc.value.translation_placeholders == {"error": "no response"}
    p.stop()


async def test_timeout_message_is_not_empty():
    client = SolityClient(None, "a", "b")
    client._token = "t"
    with patch.object(client, "_raw", AsyncMock(side_effect=asyncio.TimeoutError())):
        with pytest.raises(SolityError, match="no response from the lock"):
            await client.get_status("D")


def _login_ok():
    return 200, '{"contents": {"loginResult": 0, "token": "T", "tokenPwd": "P"}}'


async def test_session_taken_by_app_is_reported():
    client = SolityClient(None, "a", "b")
    called = []
    client.on_session_taken = lambda: called.append(1)
    raw = AsyncMock(
        side_effect=[_login_ok(), (401, ""), _login_ok(), (200, '{"contents": {}}')]
    )
    with patch.object(client, "_raw", raw):
        await client.login()
        await client.get_devices()
    assert called == [1]


async def test_first_login_401_is_not_a_conflict():
    """No token yet (or an old one) -> plain re-login, no warning."""
    client = SolityClient(None, "a", "b")
    called = []
    client.on_session_taken = lambda: called.append(1)
    client._token = "old"  # restored, age unknown
    raw = AsyncMock(side_effect=[(401, ""), _login_ok(), (200, '{"contents": {}}')])
    with patch.object(client, "_raw", raw):
        await client.get_devices()
    assert called == []


async def test_session_issue_raised_and_cleared_on_reload(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    issue_id = f"session_taken_{entry.entry_id}"
    entry.runtime_data.status.client.on_session_taken()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    p.stop()


async def test_last_unlock_sensor_name(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    assert hass.states.get(LAST) is not None
    assert hass.states.get("event.hyeongwan_door_event") is None
    p.stop()


async def _reconfigure(hass, entry, devices, login_error=None):
    with patch.multiple(
        "custom_components.solity.config_flow.SolityClient",
        login=AsyncMock(side_effect=login_error),
        get_devices=AsyncMock(return_value=devices),
    ):
        flow = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        assert flow["step_id"] == "reconfigure"
        return await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"email": "ha@example.com", "password": "pw"}
        )


async def test_reconfigure_switches_account_keeps_entities(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    result = await _reconfigure(hass, entry, [{"myDeviceId": "D"}])
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert entry.data["email"] == "ha@example.com"
    assert entry.data["device_id"] == "D"
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == "locked"  # same entity, reloaded
    p.stop()


async def test_reconfigure_rejects_account_without_the_lock(hass, freezer):
    freezer.move_to(START)
    fake = Fake()
    entry, p = await _setup(hass, fake)
    result = await _reconfigure(hass, entry, [{"myDeviceId": "OTHER"}])
    assert result["errors"] == {"base": "device_not_shared"}
    assert entry.data["email"] == "a"
    p.stop()
