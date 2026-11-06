"""The Duepi Pellet Stove integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import DuepiClient, DuepiCloudClient, DuepiConnectionError
from .const import (
    CONF_DEVICE_CODE,
    CONF_DEVICE_ID,
    CONF_DEFAULT_POWER,
    CONF_DEFAULT_TEMPERATURE,
    CONF_ECO_POWER,
    CONF_ECO_TEMPERATURE,
    CONF_SCAN_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_ECO_POWER,
    DEFAULT_ECO_TEMPERATURE,
    DEFAULT_POWER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TEMPERATURE,
    PLATFORMS,
    TRANSPORT_CLOUD,
    TRANSPORT_RELAY,
)
from .coordinator import DuepiCoordinator
from .relay import DuepiRelayClient

_LOGGER = logging.getLogger(__name__)

type DuepiConfigEntry = ConfigEntry[DuepiCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DuepiConfigEntry) -> bool:
    """Set up Duepi Pellet Stove from a config entry."""
    _LOGGER.debug("Setting up Duepi integration")

    transport = entry.data.get(CONF_TRANSPORT, TRANSPORT_CLOUD)
    client: DuepiClient
    if transport == TRANSPORT_RELAY:
        client = DuepiRelayClient(
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_DEVICE_CODE],
            # The stove does not expose its setpoint; start from the option.
            default_temperature=entry.options.get(
                CONF_DEFAULT_TEMPERATURE, DEFAULT_TEMPERATURE
            ),
        )
    else:
        # Home Assistant owns the session lifecycle. A dedicated cookie jar
        # prevents account cookies for this cloud service leaking into another
        # integration.
        session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
        client = DuepiCloudClient(
            session=session,
            email=entry.data[CONF_EMAIL],
            password=entry.data[CONF_PASSWORD],
            device_id=entry.data[CONF_DEVICE_ID],
        )

    try:
        # Only the cloud has an account to log in to; the relay logs in per call.
        if isinstance(client, DuepiCloudClient):
            _LOGGER.debug("Logging in to dpremoteiot.com")
            if not await client.async_login():
                raise ConfigEntryAuthFailed("Login failed with provided credentials")
            _LOGGER.debug("Login successful")

        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        coordinator = DuepiCoordinator(
            hass,
            entry,
            client,
            update_interval=timedelta(seconds=scan_interval),
            default_power=entry.options.get(CONF_DEFAULT_POWER, DEFAULT_POWER),
            default_temperature=entry.options.get(
                CONF_DEFAULT_TEMPERATURE, DEFAULT_TEMPERATURE
            ),
            eco_power=entry.options.get(CONF_ECO_POWER, DEFAULT_ECO_POWER),
            eco_temperature=entry.options.get(
                CONF_ECO_TEMPERATURE, DEFAULT_ECO_TEMPERATURE
            ),
        )

        _LOGGER.debug("Running first data refresh (interval=%ss)", scan_interval)
        await coordinator.async_config_entry_first_refresh()
    except DuepiConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to {client.transport_name}: {err}"
        ) from err

    _LOGGER.info("Duepi integration ready")

    entry.runtime_data = coordinator

    # Listen for options changes
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DuepiConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: DuepiCoordinator = entry.runtime_data
        coordinator.async_cancel_disconnect_grace()

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: DuepiConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
