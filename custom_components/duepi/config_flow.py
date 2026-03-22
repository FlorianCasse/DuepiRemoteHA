"""Config flow for Duepi Pellet Stove integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .api import DuepiAuthError, DuepiCloudClient, DuepiConnectionError
from .const import (
    CONF_DEFAULT_POWER,
    CONF_DEFAULT_TEMPERATURE,
    CONF_DEVICE_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_POWER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TEMPERATURE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_DEVICE_ID): str,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class DuepiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Duepi Pellet Stove."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()

            # Check if already configured
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

            # Validate credentials by attempting login + dashboard fetch
            error = await self._async_validate_credentials(
                user_input[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                device_id,
            )

            if error is None:
                return self.async_create_entry(
                    title=f"Duepi Stove ({device_id[:8]}...)",
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DEVICE_ID: device_id,
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle reauth when session/credentials fail."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth credential entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            device_id = reauth_entry.data[CONF_DEVICE_ID]

            error = await self._async_validate_credentials(
                user_input[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                device_id,
            )

            if error is None:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )

    async def _async_validate_credentials(
        self, email: str, password: str, device_id: str
    ) -> str | None:
        """Validate credentials. Returns an error key or None on success."""
        jar = aiohttp.CookieJar(unsafe=True)
        session = aiohttp.ClientSession(cookie_jar=jar)
        try:
            client = DuepiCloudClient(session, email, password, device_id)
            if not await client.async_login():
                return "invalid_auth"
            # Verify the device exists on the dashboard
            state = await client.async_get_stove_state()
            if state.room_temperature is None and state.status_text is None and not state.power_on:
                _LOGGER.warning("Device %s may not exist on dashboard", device_id)
                # Don't fail — the device might just be offline
            return None
        except DuepiAuthError:
            return "invalid_auth"
        except DuepiConnectionError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during validation")
            return "unknown"
        finally:
            await session.close()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return DuepiOptionsFlow(config_entry)


class DuepiOptionsFlow(OptionsFlow):
    """Handle options for Duepi integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=600)),
                    vol.Optional(
                        CONF_DEFAULT_POWER,
                        default=options.get(CONF_DEFAULT_POWER, DEFAULT_POWER),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
                    vol.Optional(
                        CONF_DEFAULT_TEMPERATURE,
                        default=options.get(CONF_DEFAULT_TEMPERATURE, DEFAULT_TEMPERATURE),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=35)),
                }
            ),
        )
