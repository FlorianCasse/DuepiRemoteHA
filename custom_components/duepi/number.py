"""Number entity for Duepi Pellet Stove power level."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, MAX_POWER, MIN_POWER
from .coordinator import DuepiCoordinator
from .device import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Duepi power number entity."""
    coordinator: DuepiCoordinator = entry.runtime_data
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities([DuepiPowerNumber(coordinator, device_id)])


class DuepiPowerNumber(CoordinatorEntity[DuepiCoordinator], NumberEntity):
    """Number entity for adjusting stove power level (1-5)."""

    _attr_has_entity_name = True
    _attr_translation_key = "power_level"
    _attr_icon = "mdi:fire"
    _attr_native_min_value = MIN_POWER
    _attr_native_max_value = MAX_POWER
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: DuepiCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id}_power_number"
        self._attr_device_info = build_device_info(device_id)

    @property
    def native_value(self) -> float | None:
        """Return the current power level."""
        if self.coordinator.data and self.coordinator.data.working_power is not None:
            return float(self.coordinator.data.working_power)
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the power level."""
        await self.coordinator.async_set_power(int(value))
