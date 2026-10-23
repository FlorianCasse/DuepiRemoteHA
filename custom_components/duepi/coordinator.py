"""DataUpdateCoordinator for Duepi Pellet Stove."""

from __future__ import annotations

import asyncio
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
    DuepiApiError,
    DuepiAuthError,
    DuepiCloudClient,
    DuepiConnectionError,
    DuepiParseError,
    DuepiRateLimitError,
    DuepiServerError,
    DuepiStoveState,
    DuepiTransportError,
)
from .const import (
    DEFAULT_ECO_POWER,
    DEFAULT_ECO_TEMPERATURE,
    DEFAULT_POWER,
    DEFAULT_TEMPERATURE,
    DOMAIN,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
)

_LOGGER = logging.getLogger(__name__)

# The cloud periodically reports healthy stoves offline for 60-90 seconds.
# Wait long enough to absorb that upstream heartbeat gap while still exposing
# sustained disconnects promptly.
DISCONNECT_GRACE_PERIOD = 120

# Cloud commands sent to re-apply the desired power after ignition, per turn-on.
MAX_ENFORCE_POWER_ATTEMPTS = 3


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
        eco_power: int = DEFAULT_ECO_POWER,
        eco_temperature: int = DEFAULT_ECO_TEMPERATURE,
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
        self._eco_power = eco_power
        self._eco_temperature = eco_temperature
        self._pending_preset: str | None = None
        self._desired_power: int | None = None
        self._enforce_power_pending = False
        self._enforce_seen_on = False
        self._enforce_attempts = 0
        self._turn_off_generation = 0
        self._command_lock = asyncio.Lock()
        self._has_seen_connected = False
        self._disconnect_grace_started_at: float | None = None
        self._disconnect_grace_cancel: Callable[[], None] | None = None
        self._pending_disconnect_state: DuepiStoveState | None = None
        self._disconnect_confirmed = False
        self._was_disconnected = False
        self._last_successful_update_time: datetime | None = None
        self._last_seen: datetime | None = None

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
    def last_seen(self) -> datetime | None:
        """Return the last time the cloud explicitly reported the stove online."""
        return self._last_seen

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

            # Some firmwares ignore settedPower during ignition and start at a
            # fixed level. The cloud echoes the last settedPower sent, not the
            # power the stove actually runs at, so the reported value cannot
            # reveal this. Re-send the desired power once per HA turn-on, when
            # the stove first reports nominal heating.
            is_heating = bool(
                state.status_text and "heating" in state.status_text.lower()
            )
            if self._enforce_power_pending:
                if state.power_on:
                    self._enforce_seen_on = True
                elif self._enforce_seen_on:
                    # Alarm, or switched off from the panel/app before heating.
                    _LOGGER.debug("Stove off before nominal heating; dropping power enforcement")
                    self._enforce_power_pending = False
                if self._enforce_power_pending and state.power_on and is_heating:
                    self._enforce_power_pending = False
                    _LOGGER.info("Stove reached nominal heating — enforcing desired power")
                    self.config_entry.async_create_background_task(
                        self.hass,
                        self._async_enforce_power(state, self._turn_off_generation),
                        name=f"{DOMAIN}_enforce_power",
                    )
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
            self._last_seen = datetime.now(timezone.utc)
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
        pending_preset = self._pending_preset
        targets = (
            self.preset_targets(pending_preset)
            if pending_preset is not None
            else None
        )
        power, temperature = targets or (
            self._default_power,
            self._default_temperature,
        )
        self._desired_power = power
        _LOGGER.info("Turning stove ON (power=%d, temp=%d)", power, temperature)
        await self.client.async_turn_on(power=power, temperature=temperature)
        self._enforce_power_pending = True
        self._enforce_seen_on = False
        self._enforce_attempts = 0
        self._pending_preset = None
        if state:
            self.async_set_updated_data(
                replace(
                    state,
                    power_on=True,
                    working_power=power,
                    set_temperature=temperature,
                )
            )

    async def async_turn_off(self) -> None:
        """Turn the stove off and refresh."""
        _LOGGER.info("Turning stove OFF")
        # Invalidate any scheduled enforcement first, and wait for one already
        # sending, so its active=1 command can never land after this one.
        self._enforce_power_pending = False
        self._turn_off_generation += 1
        async with self._command_lock:
            await self.client.async_turn_off()
        state = self.data
        if state:
            self.async_set_updated_data(replace(state, power_on=False))

    async def async_set_power(self, power: int) -> None:
        """Set working power and refresh."""
        self._desired_power = power
        _LOGGER.info("Setting stove power to %d", power)
        await self.client.async_set_power(power, current_state=self.data)
        state = self.data
        if state:
            self.async_set_updated_data(replace(state, working_power=power))

    async def async_set_temperature(self, temperature: int) -> None:
        """Set target temperature and refresh."""
        _LOGGER.info("Setting stove temperature to %d°C", temperature)
        await self.client.async_set_temperature(temperature, current_state=self.data)
        state = self.data
        if state:
            self.async_set_updated_data(replace(state, set_temperature=temperature))

    def preset_targets(self, preset: str) -> tuple[int, int] | None:
        """Return the configured targets for a climate preset."""
        if preset == PRESET_NONE:
            return None
        if preset == PRESET_ECO:
            return self._eco_power, self._eco_temperature
        if preset == PRESET_COMFORT:
            return self._default_power, self._default_temperature
        raise ValueError(f"Unsupported preset: {preset}")

    def current_preset(self) -> str:
        """Return the pending or currently matching climate preset."""
        if self._pending_preset is not None:
            return self._pending_preset
        state = self.data
        if state is None:
            return PRESET_NONE
        current_targets = (state.working_power, state.set_temperature)
        if current_targets == self.preset_targets(PRESET_ECO):
            return PRESET_ECO
        if current_targets == self.preset_targets(PRESET_COMFORT):
            return PRESET_COMFORT
        return PRESET_NONE

    async def async_set_preset(self, preset: str) -> None:
        """Apply a preset now or defer it until the next turn-on."""
        targets = self.preset_targets(preset)
        if targets is None:
            self._pending_preset = None
            return

        state = self.data
        if state is None or not state.power_on:
            self._pending_preset = preset
            return

        power, temperature = targets
        self._desired_power = power
        await self.client.async_turn_on(power=power, temperature=temperature)
        self.async_set_updated_data(
            replace(
                state,
                working_power=power,
                set_temperature=temperature,
            )
        )

    async def _async_enforce_power(self, state: DuepiStoveState, generation: int) -> None:
        """Re-send desired power after stove reaches nominal heating.

        The command is built from the poll that observed heating (stove on), not
        from self.data, which may still hold the previous poll at this point.
        """
        # Read at send time so a power change made meanwhile is not undone.
        power = self._desired_power
        if power is None or state.set_temperature is None:
            return
        async with self._command_lock:
            if generation != self._turn_off_generation:
                return
            try:
                await self.client.async_set_power(power, current_state=state)
            except DuepiApiError as err:
                self._enforce_attempts += 1
                if generation != self._turn_off_generation:
                    # Turned off while this command was in flight; never re-arm.
                    return
                if self._enforce_attempts < MAX_ENFORCE_POWER_ATTEMPTS:
                    _LOGGER.warning(
                        "Failed to enforce desired power %d, retrying on next poll: %s",
                        power,
                        err,
                    )
                    self._enforce_power_pending = True
                else:
                    _LOGGER.warning(
                        "Failed to enforce desired power %d after %d attempts: %s",
                        power,
                        self._enforce_attempts,
                        err,
                    )
                return
        await self.async_request_refresh()


def state_has_problem(state: DuepiStoveState | None) -> bool | None:
    """Return whether a stove state reports an alarm or error status."""
    if state is None:
        return None
    status = state.status_text.lower() if state.status_text else ""
    return bool(state.alarm) or "alarm" in status or "error" in status
