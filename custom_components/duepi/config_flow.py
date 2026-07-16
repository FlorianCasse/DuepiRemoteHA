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
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    DuepiAuthError,
    DuepiCloudClient,
    DuepiConnectionError,
    DuepiDeviceSummary,
    DuepiParseError,
    DuepiRateLimitError,
    DuepiServerError,
    DuepiTransportError,
)
from .const import (
    CONF_DEFAULT_POWER,
    CONF_DEFAULT_TEMPERATURE,
    CONF_DEVICE_ID,
    CONF_ECO_POWER,
    CONF_ECO_TEMPERATURE,
    CONF_PELLET_KG_PER_HOUR_MAX,
    CONF_PELLET_KG_PER_HOUR_MIN,
    CONF_SCAN_INTERVAL,
    DEFAULT_ECO_POWER,
    DEFAULT_ECO_TEMPERATURE,
    DEFAULT_PELLET_KG_PER_HOUR_MAX,
    DEFAULT_PELLET_KG_PER_HOUR_MIN,
    DEFAULT_POWER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TEMPERATURE,
    DOMAIN,
    MAX_POWER,
    MAX_TEMPERATURE,
    MIN_POWER,
    MIN_TEMPERATURE,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

MANUAL_DEVICE_SCHEMA = vol.Schema({vol.Required(CONF_DEVICE_ID): str})

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class DuepiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Duepi Pellet Stove."""

    VERSION = 1
    _client: DuepiCloudClient | None = None
    _devices: dict[str, DuepiDeviceSummary]
    _email: str | None = None
    _password: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            session = async_create_clientsession(
                self.hass,
                cookie_jar=aiohttp.CookieJar(),
            )
            self._client = DuepiCloudClient(
                session,
                self._email,
                self._password,
            )

            try:
                if not await self._client.async_login():
                    errors["base"] = "invalid_auth"
                else:
                    devices = await self._client.async_list_devices()
                    self._devices = {device.device_id: device for device in devices}
                    if devices:
                        return await self.async_step_select_device()
                    return await self.async_step_manual_device()
            except DuepiParseError:
                self._devices = {}
                return await self.async_step_manual_device()
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._error_from_exception(err)

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select a discovered stove."""
        if self._client is None or not getattr(self, "_devices", None):
            return await self.async_step_user()

        errors: dict[str, str] = {}
        choices = {
            device_id: (
                f"{summary.name} ({device_id})" if summary.name else device_id
            )
            for device_id, summary in self._devices.items()
        }

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()
            summary = self._devices.get(device_id)
            if summary is None:
                errors["base"] = "invalid_device"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()
                self._client.select_device(device_id, summary.api_id)
                error = await self._async_validate_client(self._client, login=False)
                if error is None:
                    return self._create_config_entry(device_id, summary.name)
                errors["base"] = error

        first_device_id = next(iter(choices))
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_ID,
                        default=first_device_id,
                    ): vol.In(choices)
                }
            ),
            errors=errors,
        )

    async def async_step_manual_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Enter a stove identifier when discovery is unavailable."""
        if self._client is None:
            return await self.async_step_user()

        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            self._client.select_device(device_id)
            error = await self._async_validate_client(self._client, login=False)
            if error is None:
                return self._create_config_entry(device_id)
            errors["base"] = error

        return self.async_show_form(
            step_id="manual_device",
            data_schema=MANUAL_DEVICE_SCHEMA,
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
        session = async_create_clientsession(
            self.hass,
            cookie_jar=aiohttp.CookieJar(),
        )
        client = DuepiCloudClient(session, email, password, device_id)
        return await self._async_validate_client(client)

    async def _async_validate_client(
        self, client: DuepiCloudClient, *, login: bool = True
    ) -> str | None:
        """Validate a client and map failures to config-flow error keys."""
        try:
            if login and not await client.async_login():
                return "invalid_auth"
            await client.async_get_stove_state()
            return None
        except Exception as err:  # noqa: BLE001
            return self._error_from_exception(err)

    @staticmethod
    def _error_from_exception(err: Exception) -> str:
        """Map a cloud exception to a config-flow error key."""
        if isinstance(err, DuepiAuthError):
            return "invalid_auth"
        if isinstance(err, DuepiParseError):
            return "invalid_device"
        if isinstance(err, DuepiTransportError):
            return "cannot_connect"
        if isinstance(err, DuepiServerError):
            return "server_error"
        if isinstance(err, DuepiRateLimitError):
            return "rate_limited"
        if isinstance(err, DuepiConnectionError):
            return "unknown"
        _LOGGER.exception("Unexpected error during validation", exc_info=err)
        return "unknown"

    def _create_config_entry(
        self, device_id: str, name: str | None = None
    ) -> FlowResult:
        """Create an entry using the stable version-1 data shape."""
        if self._email is None or self._password is None:
            _LOGGER.exception("Unexpected error during validation")
            raise RuntimeError("Config-flow credentials are unavailable")

        return self.async_create_entry(
            title=name or f"Duepi Stove ({device_id[:8]}...)",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_DEVICE_ID: device_id,
            },
        )

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
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input[CONF_PELLET_KG_PER_HOUR_MIN]
                > user_input[CONF_PELLET_KG_PER_HOUR_MAX]
            ):
                errors["base"] = "invalid_pellet_range"
            else:
                return self.async_create_entry(data=user_input)

        options = user_input or self.config_entry.options
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
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POWER, max=MAX_POWER)),
                    vol.Optional(
                        CONF_DEFAULT_TEMPERATURE,
                        default=options.get(CONF_DEFAULT_TEMPERATURE, DEFAULT_TEMPERATURE),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_TEMPERATURE, max=MAX_TEMPERATURE),
                    ),
                    vol.Optional(
                        CONF_ECO_POWER,
                        default=options.get(CONF_ECO_POWER, DEFAULT_ECO_POWER),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POWER, max=MAX_POWER)),
                    vol.Optional(
                        CONF_ECO_TEMPERATURE,
                        default=options.get(
                            CONF_ECO_TEMPERATURE, DEFAULT_ECO_TEMPERATURE
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_TEMPERATURE, max=MAX_TEMPERATURE),
                    ),
                    vol.Optional(
                        CONF_PELLET_KG_PER_HOUR_MIN,
                        default=options.get(
                            CONF_PELLET_KG_PER_HOUR_MIN,
                            DEFAULT_PELLET_KG_PER_HOUR_MIN,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                    vol.Optional(
                        CONF_PELLET_KG_PER_HOUR_MAX,
                        default=options.get(
                            CONF_PELLET_KG_PER_HOUR_MAX,
                            DEFAULT_PELLET_KG_PER_HOUR_MAX,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                }
            ),
            errors=errors,
        )
