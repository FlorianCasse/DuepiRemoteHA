"""Binary sensor entity for Duepi Pellet Stove."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID
from .coordinator import DuepiCoordinator
from .device import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Duepi online binary sensor."""
    coordinator: DuepiCoordinator = entry.runtime_data
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities([DuepiOnlineBinarySensor(coordinator, device_id)])


class DuepiOnlineBinarySensor(CoordinatorEntity[DuepiCoordinator], BinarySensorEntity):
    """Binary sensor indicating whether the stove is online."""

    _attr_has_entity_name = True
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DuepiCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id}_online"
        self._attr_device_info = build_device_info(device_id)

    @property
    def is_on(self) -> bool | None:
        """Return True if the stove is online."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.online
