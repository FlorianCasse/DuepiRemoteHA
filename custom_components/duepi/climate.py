"""Climate entity for Duepi Pellet Stove."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, MAX_TEMPERATURE, MIN_POWER, MAX_POWER, MIN_TEMPERATURE
from .coordinator import DuepiCoordinator
from .device import build_device_info

FAN_MODES = [str(i) for i in range(MIN_POWER, MAX_POWER + 1)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Duepi climate entity."""
    coordinator: DuepiCoordinator = entry.runtime_data
    async_add_entities([DuepiClimateEntity(coordinator, entry)])


class DuepiClimateEntity(CoordinatorEntity[DuepiCoordinator], ClimateEntity):
    """Climate entity for Duepi pellet stove."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_min_temp = MIN_TEMPERATURE
    _attr_max_temp = MAX_TEMPERATURE
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_fan_modes = FAN_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: DuepiCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_climate"
        self._attr_device_info = build_device_info(device_id)

    @property
    def available(self) -> bool:
        """Return True if the stove is reachable."""
        if not super().available:
            return False
        if self.coordinator.data and self.coordinator.data.online is False:
            return False
        return True

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        if self.coordinator.data and self.coordinator.data.power_on:
            return HVACMode.HEAT
        return HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        data = self.coordinator.data
        if not data or not data.power_on:
            return HVACAction.OFF

        if data.status_text:
            status_lower = data.status_text.lower()
            if "heating" in status_lower:
                return HVACAction.HEATING
            if "idle" in status_lower or "standby" in status_lower:
                return HVACAction.IDLE
        return HVACAction.HEATING

    @property
    def current_temperature(self) -> float | None:
        """Return the stove's room temperature reading."""
        if self.coordinator.data:
            return self.coordinator.data.room_temperature
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        if self.coordinator.data:
            return self.coordinator.data.set_temperature
        return None

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan mode (power level)."""
        if self.coordinator.data and self.coordinator.data.working_power is not None:
            return str(self.coordinator.data.working_power)
        return None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (heat or off)."""
        if hvac_mode == HVACMode.HEAT:
            await self.coordinator.async_turn_on()
        elif hvac_mode == HVACMode.OFF:
            await self.coordinator.async_turn_off()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            await self.coordinator.async_set_temperature(int(temperature))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode (power level 1-5)."""
        await self.coordinator.async_set_power(int(fan_mode))

    async def async_turn_on(self) -> None:
        """Turn the stove on."""
        await self.coordinator.async_turn_on()

    async def async_turn_off(self) -> None:
        """Turn the stove off."""
        await self.coordinator.async_turn_off()
