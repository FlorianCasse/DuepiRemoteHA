"""DataUpdateCoordinator for Duepi Pellet Stove."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DuepiAuthError,
    DuepiCloudClient,
    DuepiConnectionError,
    DuepiParseError,
    DuepiStoveState,
)
from .const import DEFAULT_POWER, DEFAULT_TEMPERATURE, DOMAIN

_LOGGER = logging.getLogger(__name__)


class DuepiCoordinator(DataUpdateCoordinator[DuepiStoveState]):
    """Coordinator that polls dpremoteiot.com for stove state."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: DuepiCloudClient,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = client

    async def _async_update_data(self) -> DuepiStoveState:
        """Fetch stove state from dpremoteiot.com."""
        try:
            return await self.client.async_get_stove_state()
        except DuepiAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed. Please re-enter your credentials."
            ) from err
        except DuepiConnectionError as err:
            raise UpdateFailed(f"Cannot connect to dpremoteiot.com: {err}") from err
        except DuepiParseError as err:
            raise UpdateFailed(f"Failed to parse stove data: {err}") from err

    async def async_turn_on(self) -> None:
        """Turn the stove on and refresh."""
        state = self.data
        power = state.working_power if state else DEFAULT_POWER
        temperature = state.set_temperature if state else DEFAULT_TEMPERATURE
        await self.client.async_turn_on(power=power, temperature=temperature)
        await self.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn the stove off and refresh."""
        await self.client.async_turn_off()
        await self.async_request_refresh()

    async def async_set_power(self, power: int) -> None:
        """Set working power and refresh."""
        await self.client.async_set_power(power, current_state=self.data)
        await self.async_request_refresh()

    async def async_set_temperature(self, temperature: int) -> None:
        """Set target temperature and refresh."""
        await self.client.async_set_temperature(temperature, current_state=self.data)
        await self.async_request_refresh()
