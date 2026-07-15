"""DataUpdateCoordinator for Duepi Pellet Stove."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DuepiAuthError,
    DuepiCloudClient,
    DuepiConnectionError,
    DuepiParseError,
    DuepiRateLimitError,
    DuepiServerError,
    DuepiStoveState,
    DuepiTransportError,
)
from .const import DEFAULT_POWER, DEFAULT_TEMPERATURE, DOMAIN

_LOGGER = logging.getLogger(__name__)

# The cloud periodically reports healthy stoves offline for 60-90 seconds.
# Wait long enough to absorb that upstream heartbeat gap while still exposing
# sustained disconnects promptly.
DISCONNECT_GRACE_PERIOD = 120


class DuepiCoordinator(DataUpdateCoordinator[DuepiStoveState]):
    """Coordinator that polls dpremoteiot.com for stove state."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: DuepiCloudClient,
        update_interval: timedelta,
        *,
        default_power: int = DEFAULT_POWER,
        default_temperature: int = DEFAULT_TEMPERATURE,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.config_entry = config_entry
        self.client = client
        self._default_power = default_power
        self._default_temperature = default_temperature
        self._desired_power: int | None = None
        self._was_heating: bool = False
        self._has_seen_connected = False
        self._disconnect_grace_started_at: float | None = None
        self._disconnect_grace_cancel: Callable[[], None] | None = None
        self._pending_disconnect_state: DuepiStoveState | None = None
        self._disconnect_confirmed = False
        self._was_disconnected = False
        self._last_successful_update_time: datetime | None = None

    @property
    def raw_online(self) -> bool | None:
        """Return the most recently reported, unfiltered connectivity state."""
        return self.data.raw_online if self.data else None

    @property
    def filtered_online(self) -> bool | None:
        """Return the connectivity state published to entities."""
        return self.data.online if self.data else None

    @property
    def last_successful_update_time(self) -> datetime | None:
        """Return when a stove-state poll most recently completed successfully."""
        return self._last_successful_update_time

    @property
    def disconnect_grace_started_at(self) -> float | None:
        """Return the monotonic timestamp at which the current grace period began."""
        return self._disconnect_grace_started_at

    @property
    def disconnect_grace_deadline(self) -> float | None:
        """Return the monotonic timestamp at which a pending disconnect is confirmed."""
        if self._disconnect_grace_started_at is None:
            return None
        return self._disconnect_grace_started_at + DISCONNECT_GRACE_PERIOD

    @property
    def disconnect_elapsed_seconds(self) -> float | None:
        """Return the duration of the current raw-offline period."""
        if self._disconnect_grace_started_at is None:
            return None
        return max(0.0, monotonic() - self._disconnect_grace_started_at)

    async def _async_update_data(self) -> DuepiStoveState:
        """Fetch stove state from dpremoteiot.com."""
        _LOGGER.debug("Polling stove state")
        try:
            state = await self.client.async_get_stove_state()
            state = self._filter_connectivity(state)
            _LOGGER.debug(
                "Stove state: on=%s, status=%s, room=%s°C, set=%s°C, power=%s",
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
                    self.config_entry.async_create_background_task(
                        self.hass,
                        self._async_enforce_power(self._desired_power),
                        name=f"{DOMAIN}_enforce_power",
                    )
            self._was_heating = is_heating
            self._last_successful_update_time = datetime.now(timezone.utc)

            return state
        except DuepiAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed. Please re-enter your credentials."
            ) from err
        except DuepiTransportError as err:
            raise UpdateFailed(f"Cannot connect to dpremoteiot.com: {err}") from err
        except DuepiServerError as err:
            raise UpdateFailed(f"Cloud service server error: {err}") from err
        except DuepiRateLimitError as err:
            raise UpdateFailed(f"Cloud service rate limit: {err}") from err
        except DuepiConnectionError as err:
            raise UpdateFailed(f"Cloud service request failed: {err}") from err
        except DuepiParseError as err:
            raise UpdateFailed(f"Failed to parse stove data: {err}") from err

    def _filter_connectivity(self, state: DuepiStoveState) -> DuepiStoveState:
        """Apply a disconnect grace period without hiding raw connectivity."""
        raw_online = state.raw_online

        if raw_online is True:
            was_disconnected = (
                self._disconnect_grace_started_at is not None
                or self._disconnect_confirmed
                or self._was_disconnected
                or (self.data is not None and self.data.online is False)
            )
            self._has_seen_connected = True
            self._reset_disconnect_grace()
            if was_disconnected:
                _LOGGER.info("Stove connectivity restored")
            return replace(state, online=True)

        if raw_online is False:
            if not self._has_seen_connected:
                self._was_disconnected = True
                return replace(state, online=False)

            now = monotonic()
            if self._disconnect_grace_started_at is None:
                self._disconnect_grace_started_at = now
                self._pending_disconnect_state = state
                self._disconnect_grace_cancel = async_call_later(
                    self.hass,
                    DISCONNECT_GRACE_PERIOD,
                    self._async_confirm_disconnect,
                )
                _LOGGER.info(
                    "Stove reported offline; waiting %d seconds before marking disconnected",
                    DISCONNECT_GRACE_PERIOD,
                )
            else:
                self._pending_disconnect_state = state

            if now - self._disconnect_grace_started_at >= DISCONNECT_GRACE_PERIOD:
                if not self._disconnect_confirmed:
                    self._cancel_disconnect_timer()
                    _LOGGER.info("Stove disconnect confirmed after grace period")
                    self._disconnect_confirmed = True
                    self._was_disconnected = True
                return replace(state, online=False)

            return replace(state, online=True)

        # An unknown report interrupts a continuous raw-offline period but is not offline.
        self._reset_disconnect_grace(clear_disconnected=False)
        return replace(state, online=None)

    @callback
    def _async_confirm_disconnect(self, _now: datetime) -> None:
        """Publish a pending raw disconnect when its grace period ends."""
        if self._disconnect_confirmed or self._pending_disconnect_state is None:
            return

        self._disconnect_grace_cancel = None
        self._disconnect_confirmed = True
        self._was_disconnected = True
        _LOGGER.info("Stove disconnect confirmed after grace period")
        state = self.data or self._pending_disconnect_state
        self.async_set_updated_data(replace(state, online=False))

    def _reset_disconnect_grace(self, *, clear_disconnected: bool = True) -> None:
        """Cancel a pending disconnect timer and clear its state."""
        self._cancel_disconnect_timer()
        self._disconnect_grace_started_at = None
        self._pending_disconnect_state = None
        self._disconnect_confirmed = False
        if clear_disconnected:
            self._was_disconnected = False

    def async_cancel_disconnect_grace(self) -> None:
        """Cancel a delayed disconnect callback during coordinator teardown."""
        self._reset_disconnect_grace()

    def _cancel_disconnect_timer(self) -> None:
        """Cancel the one-shot delayed disconnect callback, if any."""
        if self._disconnect_grace_cancel is not None:
            self._disconnect_grace_cancel()
        self._disconnect_grace_cancel = None

    async def async_turn_on(self) -> None:
        """Turn the stove on and refresh."""
        state = self.data
        power = self._default_power
        temperature = self._default_temperature
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
                    raw_online=state.raw_online,
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
                    raw_online=state.raw_online,
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
                    raw_online=state.raw_online,
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
                    raw_online=state.raw_online,
                    online=state.online,
                )
            )

    async def _async_enforce_power(self, power: int) -> None:
        """Re-send desired power after stove reaches nominal heating."""
        await self.client.async_set_power(power, current_state=self.data)
        await self.async_request_refresh()
