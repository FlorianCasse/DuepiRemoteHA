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
        self._desired_power: int | None = None
        self._was_heating: bool = False

    async def _async_update_data(self) -> DuepiStoveState:
        """Fetch stove state from dpremoteiot.com."""
        _LOGGER.debug("Polling stove state")
        try:
            state = await self.client.async_get_stove_state()
            _LOGGER.debug(
                "Stove state: on=%s, status=%s, room=%.1f°C, set=%d°C, power=%d",
                state.power_on,
                state.status_text,
                state.room_temperature,
                state.set_temperature,
                state.working_power,
            )

            # Detect transition to nominal heating and enforce desired power
            is_heating = bool(
                state.status_text and "heating" in state.status_text.lower()
            )
            if is_heating and not self._was_heating:
                if (
                    self._desired_power is not None
                    and state.working_power != self._desired_power
                ):
                    _LOGGER.info(
                        "Stove reached nominal heating — enforcing desired power %d (reported %d)",
                        self._desired_power,
                        state.working_power,
                    )
                    self.hass.async_create_task(
                        self._async_enforce_power(self._desired_power)
                    )
            self._was_heating = is_heating

            return state
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
        self._desired_power = power
        _LOGGER.info("Turning stove ON (power=%d, temp=%d)", power, temperature)
        await self.client.async_turn_on(power=power, temperature=temperature)
        if state:
            self.async_set_updated_data(
                DuepiStoveState(
                    power_on=True,
                    status_text=state.status_text,
                    room_temperature=state.room_temperature,
                    working_power=power,
                    set_temperature=temperature,
                    online=state.online,
                )
            )

    async def async_turn_off(self) -> None:
        """Turn the stove off and refresh."""
        _LOGGER.info("Turning stove OFF")
        await self.client.async_turn_off()
        state = self.data
        if state:
            self.async_set_updated_data(
                DuepiStoveState(
                    power_on=False,
                    status_text=state.status_text,
                    room_temperature=state.room_temperature,
                    working_power=state.working_power,
                    set_temperature=state.set_temperature,
                    online=state.online,
                )
            )

    async def async_set_power(self, power: int) -> None:
        """Set working power and refresh."""
        self._desired_power = power
        _LOGGER.info("Setting stove power to %d", power)
        await self.client.async_set_power(power, current_state=self.data)
        state = self.data
        if state:
            self.async_set_updated_data(
                DuepiStoveState(
                    power_on=state.power_on,
                    status_text=state.status_text,
                    room_temperature=state.room_temperature,
                    working_power=power,
                    set_temperature=state.set_temperature,
                    online=state.online,
                )
            )

    async def async_set_temperature(self, temperature: int) -> None:
        """Set target temperature and refresh."""
        _LOGGER.info("Setting stove temperature to %d°C", temperature)
        await self.client.async_set_temperature(temperature, current_state=self.data)
        state = self.data
        if state:
            self.async_set_updated_data(
                DuepiStoveState(
                    power_on=state.power_on,
                    status_text=state.status_text,
                    room_temperature=state.room_temperature,
                    working_power=state.working_power,
                    set_temperature=temperature,
                    online=state.online,
                )
            )

    async def _async_enforce_power(self, power: int) -> None:
        """Re-send desired power after stove reaches nominal heating."""
        await self.client.async_set_power(power, current_state=self.data)
        await self.async_request_refresh()
