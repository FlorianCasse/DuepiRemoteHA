"""The Duepi Pellet Stove integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .api import DuepiCloudClient
from .const import (
    CONF_DEVICE_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import DuepiCoordinator

_LOGGER = logging.getLogger(__name__)

type DuepiConfigEntry = ConfigEntry[DuepiCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DuepiConfigEntry) -> bool:
    """Set up Duepi Pellet Stove from a config entry."""
    _LOGGER.debug("Setting up Duepi integration for device %s", entry.data[CONF_DEVICE_ID])

    # Create a dedicated cookie jar for this integration instance
    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(cookie_jar=jar)

    client = DuepiCloudClient(
        session=session,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        device_id=entry.data[CONF_DEVICE_ID],
    )

    # Initial login
    _LOGGER.debug("Logging in to dpremoteiot.com")
    await client.async_login()
    _LOGGER.debug("Login successful")

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = DuepiCoordinator(
        hass,
        client,
        update_interval=timedelta(seconds=scan_interval),
    )

    _LOGGER.debug("Running first data refresh (interval=%ss)", scan_interval)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.info("Duepi integration ready for device %s", entry.data[CONF_DEVICE_ID])

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
        await coordinator.client.async_close()

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: DuepiConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
