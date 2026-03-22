"""Sensor entities for Duepi Pellet Stove."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DuepiStoveState
from .const import CONF_DEVICE_ID
from .coordinator import DuepiCoordinator
from .device import build_device_info


@dataclass(frozen=True, kw_only=True)
class DuepiSensorDescription(SensorEntityDescription):
    """Describe a Duepi sensor."""

    value_fn: Callable[[DuepiStoveState], str | int | float | None]


SENSOR_DESCRIPTIONS: tuple[DuepiSensorDescription, ...] = (
    DuepiSensorDescription(
        key="room_temperature",
        translation_key="room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: s.room_temperature,
    ),
    DuepiSensorDescription(
        key="power_level",
        translation_key="power_level",
        icon="mdi:fire",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.working_power,
    ),
    DuepiSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.status_text,
    ),
    DuepiSensorDescription(
        key="set_temperature",
        translation_key="set_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.set_temperature,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Duepi sensor entities."""
    coordinator: DuepiCoordinator = entry.runtime_data
    device_id = entry.data[CONF_DEVICE_ID]
    async_add_entities(
        DuepiSensorEntity(coordinator, device_id, desc)
        for desc in SENSOR_DESCRIPTIONS
    )


class DuepiSensorEntity(CoordinatorEntity[DuepiCoordinator], SensorEntity):
    """Sensor entity for Duepi stove data."""

    entity_description: DuepiSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DuepiCoordinator,
        device_id: str,
        description: DuepiSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_device_info = build_device_info(device_id)

    @property
    def native_value(self) -> str | int | float | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
