"""Config flow for Smart Solity."""
from __future__ import annotations

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
from .calibration import SolityCalibration
from .const import (
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_HASHED_PWD,
    CONF_NICKNAME,
    CONF_PASSWORD,
    CONF_AUTO_CALIBRATE,
    CONF_FIRST_VERIFY_SECONDS,
    CONF_LOG_SECONDS,
    CONF_RECALIBRATE,
    CONF_STATUS_MINUTES,
    DEFAULT_AUTO_CALIBRATE,
    DEFAULT_FIRST_VERIFY_SECONDS,
    DEFAULT_LOG_SECONDS,
    DEFAULT_STATUS_MINUTES,
    MAX_FIRST_VERIFY_SECONDS,
    MAX_LOG_SECONDS,
    MAX_STATUS_MINUTES,
    MIN_LOG_SECONDS,
    MIN_STATUS_MINUTES,
    DOMAIN,
)

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SolityOptionsFlow()


class SolityOptionsFlow(OptionsFlow):
    """Options: poll intervals, auto-calibration, manual first verify."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = dict(user_input)
            if data.pop(CONF_RECALIBRATE, False):
                runtime = getattr(self.config_entry, "runtime_data", None)
                if runtime is not None:
                    await runtime.status.calibration.async_reset()
                    runtime.status.async_update_listeners()
                else:  # entry not loaded: wipe the stored calibration directly
                    await SolityCalibration(
                        self.hass, self.config_entry.entry_id
                    ).async_reset()
            return self.async_create_entry(title="", data=data)

        opts = self.config_entry.options
        log_default = opts.get(CONF_LOG_SECONDS, DEFAULT_LOG_SECONDS)
        status_default = opts.get(CONF_STATUS_MINUTES, DEFAULT_STATUS_MINUTES)
        calibrate_default = opts.get(CONF_AUTO_CALIBRATE, DEFAULT_AUTO_CALIBRATE)
        first_default = opts.get(CONF_FIRST_VERIFY_SECONDS, DEFAULT_FIRST_VERIFY_SECONDS)
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
                    vol.Required(CONF_AUTO_CALIBRATE, default=calibrate_default): bool,
                    vol.Required(CONF_RECALIBRATE, default=False): bool,
                    vol.Required(
                        CONF_FIRST_VERIFY_SECONDS, default=first_default
                    ): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=0, max=MAX_FIRST_VERIFY_SECONDS),
                    ),
                }
            ),
        )
