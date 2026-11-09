"""Config flow for Solity BLE (direct)."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .ble import SolityBleError, parse_app_key
from .const import (
    CONF_APP_KEY,
    CONF_MAC,
    CONF_NAME,
    CONF_STATUS_MINUTES,
    DEFAULT_STATUS_MINUTES,
    DOMAIN,
    MAX_STATUS_MINUTES,
    MIN_STATUS_MINUTES,
)

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="현관 도어락 (BLE)"): str,
        vol.Required(CONF_MAC): str,
        vol.Required(CONF_APP_KEY): str,
    }
)


class SolityBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Solity BLE config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            mac = user_input[CONF_MAC].strip().upper()
            app_key = user_input[CONF_APP_KEY].strip()
            if not _MAC_RE.match(mac):
                errors[CONF_MAC] = "invalid_mac"
            else:
                try:
                    parse_app_key(app_key)
                except SolityBleError:
                    errors[CONF_APP_KEY] = "invalid_app_key"
            if not errors:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or "Solity Doorlock (BLE)",
                    data={
                        CONF_NAME: user_input[CONF_NAME].strip(),
                        CONF_MAC: mac,
                        CONF_APP_KEY: app_key,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SolityBleOptionsFlow()


class SolityBleOptionsFlow(OptionsFlow):
    """Options: how often to read lock status over BLE."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_STATUS_MINUTES, DEFAULT_STATUS_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATUS_MINUTES, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_STATUS_MINUTES, max=MAX_STATUS_MINUTES),
                    ),
                }
            ),
        )
