"""Config flow for Smart Solity."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolityAuthError, SolityClient, SolityError, hash_password
from .const import (
    CONF_API_DEVICE_ID,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_HASHED_PWD,
    CONF_NICKNAME,
    CONF_PASSWORD,
    CONF_LOG_SECONDS,
    CONF_STATUS_MINUTES,
    DEFAULT_LOG_SECONDS,
    DEFAULT_STATUS_MINUTES,
    MAX_LOG_SECONDS,
    MAX_STATUS_MINUTES,
    MIN_LOG_SECONDS,
    MIN_STATUS_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class SolityConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Smart Solity config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._hashed: str | None = None
        self._devices: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._hashed = hash_password(user_input[CONF_PASSWORD])
            session = async_get_clientsession(self.hass)
            client = SolityClient(session, self._email, self._hashed)
            try:
                await client.login()
                self._devices = await client.get_devices()
            except SolityAuthError:
                errors["base"] = "invalid_auth"
            except SolityError:
                errors["base"] = "cannot_connect"
            else:
                if not self._devices:
                    errors["base"] = "no_devices"
                elif len(self._devices) == 1:
                    return await self._create(self._devices[0])
                else:
                    return await self.async_step_select_device()

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            chosen = next(
                d for d in self._devices if d.get("myDeviceId") == user_input[CONF_DEVICE_ID]
            )
            return await self._create(chosen)

        options = {
            d.get("myDeviceId"): (d.get("myDeviceNickName") or d.get("myDeviceId"))
            for d in self._devices
        }
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): vol.In(options)}),
        )

    async def _create(self, device: dict) -> ConfigFlowResult:
        device_id = device.get("myDeviceId")
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()
        nickname = device.get("myDeviceNickName") or "Solity Doorlock"
        return self.async_create_entry(
            title=nickname,
            data={
                CONF_EMAIL: self._email,
                CONF_HASHED_PWD: self._hashed,
                CONF_DEVICE_ID: device_id,
                CONF_NICKNAME: nickname,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch the account (e.g. to an invited member account for HA).

        The door lock stays the same, so entities and their IDs are kept. A
        member account may know the lock under another id; then that id is
        stored for API calls only (CONF_API_DEVICE_ID).
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL]
            hashed = hash_password(user_input[CONF_PASSWORD])
            client = SolityClient(async_get_clientsession(self.hass), email, hashed)
            try:
                await client.login()
                devices = await client.get_devices()
            except SolityAuthError:
                errors["base"] = "invalid_auth"
            except SolityError:
                errors["base"] = "cannot_connect"
            else:
                self._email, self._hashed, self._devices = email, hashed, devices
                ids = [d.get("myDeviceId") for d in devices]
                _LOGGER.info(
                    "Smart Solity reconfigure: account has %s lock(s): %s",
                    len(devices),
                    [
                        (d.get("myDeviceId"), d.get("myDeviceNickName"), d.get("_list"))
                        for d in devices
                    ],
                )
                if entry.data[CONF_DEVICE_ID] in ids:
                    return self._finish_reconfigure(None)
                if not devices:
                    errors["base"] = "device_not_shared"
                elif len(devices) == 1:
                    return self._finish_reconfigure(ids[0])
                else:
                    return await self.async_step_reconfigure_device()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: entry.data.get(CONF_EMAIL)}
            ),
            errors=errors,
        )

    async def async_step_reconfigure_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The new account has several locks under other ids: pick this one."""
        if user_input is not None:
            return self._finish_reconfigure(user_input[CONF_DEVICE_ID])
        options = {
            d.get("myDeviceId"): (d.get("myDeviceNickName") or d.get("myDeviceId"))
            for d in self._devices
        }
        return self.async_show_form(
            step_id="reconfigure_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): vol.In(options)}),
        )

    @callback
    def _finish_reconfigure(self, api_device_id: str | None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if api_device_id == entry.data[CONF_DEVICE_ID]:
            api_device_id = None
        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                CONF_EMAIL: self._email,
                CONF_HASHED_PWD: self._hashed,
                CONF_API_DEVICE_ID: api_device_id,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SolityOptionsFlow()


class SolityOptionsFlow(OptionsFlow):
    """Options: poll intervals."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        log_default = opts.get(CONF_LOG_SECONDS, DEFAULT_LOG_SECONDS)
        status_default = opts.get(CONF_STATUS_MINUTES, DEFAULT_STATUS_MINUTES)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOG_SECONDS, default=log_default): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_LOG_SECONDS, max=MAX_LOG_SECONDS),
                    ),
                    vol.Required(CONF_STATUS_MINUTES, default=status_default): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_STATUS_MINUTES, max=MAX_STATUS_MINUTES),
                    ),
                }
            ),
        )
