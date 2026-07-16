"""Sensor entities for Duepi Pellet Stove."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Literal

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .accumulation import capped_elapsed_seconds, pellet_rate
from .const import (
    CONF_DEVICE_ID,
    CONF_PELLET_KG_PER_HOUR_MAX,
    CONF_PELLET_KG_PER_HOUR_MIN,
    DEFAULT_PELLET_KG_PER_HOUR_MAX,
    DEFAULT_PELLET_KG_PER_HOUR_MIN,
)
from .coordinator import DuepiCoordinator
from .device import build_device_info


@dataclass(frozen=True, kw_only=True)
class DuepiSensorDescription(SensorEntityDescription):
    """Describe a Duepi sensor."""

    value_fn: Callable[
        [DuepiCoordinator], str | int | float | datetime | None
    ]


SENSOR_DESCRIPTIONS: tuple[DuepiSensorDescription, ...] = (
    DuepiSensorDescription(
        key="room_temperature",
        translation_key="room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda coordinator: coordinator.data.room_temperature,
    ),
    DuepiSensorDescription(
        key="power_level",
        translation_key="power_level",
        icon="mdi:fire",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.data.working_power,
    ),
    DuepiSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.data.status_text,
    ),
    DuepiSensorDescription(
        key="set_temperature",
        translation_key="set_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.data.set_temperature,
    ),
    DuepiSensorDescription(
        key="alarm",
        translation_key="alarm",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.data.alarm,
    ),
    DuepiSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.last_seen,
    ),
)

OPERATING_HOURS_DESCRIPTION = SensorEntityDescription(
    key="operating_hours",
    translation_key="operating_hours",
    icon="mdi:timer-outline",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.HOURS,
    state_class=SensorStateClass.TOTAL_INCREASING,
)

PELLET_CONSUMPTION_DESCRIPTION = SensorEntityDescription(
    key="pellet_consumption",
    translation_key="pellet_consumption",
    icon="mdi:chart-bell-curve-cumulative",
    device_class=SensorDeviceClass.WEIGHT,
    native_unit_of_measurement=UnitOfMass.KILOGRAMS,
    state_class=SensorStateClass.TOTAL_INCREASING,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Duepi sensor entities."""
    coordinator: DuepiCoordinator = entry.runtime_data
    device_id = entry.data[CONF_DEVICE_ID]
    entities: list[SensorEntity] = [
        DuepiSensorEntity(coordinator, device_id, desc)
        for desc in SENSOR_DESCRIPTIONS
    ]
    entities.extend(
        [
            DuepiAccumulatingSensor(
                coordinator,
                device_id,
                OPERATING_HOURS_DESCRIPTION,
                kind="hours",
            ),
            DuepiAccumulatingSensor(
                coordinator,
                device_id,
                PELLET_CONSUMPTION_DESCRIPTION,
                kind="pellets",
                kg_per_hour_min=entry.options.get(
                    CONF_PELLET_KG_PER_HOUR_MIN,
                    DEFAULT_PELLET_KG_PER_HOUR_MIN,
                ),
                kg_per_hour_max=entry.options.get(
                    CONF_PELLET_KG_PER_HOUR_MAX,
                    DEFAULT_PELLET_KG_PER_HOUR_MAX,
                ),
            ),
        ]
    )
    async_add_entities(entities)


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
    def native_value(self) -> str | int | float | datetime | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator)


class DuepiAccumulatingSensor(
    CoordinatorEntity[DuepiCoordinator], RestoreSensor
):
    """Restore and accumulate operating time or estimated pellet use."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DuepiCoordinator,
        device_id: str,
        description: SensorEntityDescription,
        *,
        kind: Literal["hours", "pellets"],
        kg_per_hour_min: float = DEFAULT_PELLET_KG_PER_HOUR_MIN,
        kg_per_hour_max: float = DEFAULT_PELLET_KG_PER_HOUR_MAX,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_device_info = build_device_info(device_id)
        self._kind = kind
        self._kg_per_hour_min = kg_per_hour_min
        self._kg_per_hour_max = kg_per_hour_max
        self._total = 0.0
        self._last_tick: float | None = None
        self._previous_active = False
        self._previous_power: int | None = None
        update_interval = coordinator.update_interval
        self._scan_interval_seconds = (
            update_interval.total_seconds() if update_interval is not None else 30.0
        )

    async def async_added_to_hass(self) -> None:
        """Restore the total and start timing from this process lifetime."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_sensor_data()
        if restored is not None and restored.native_value is not None:
            try:
                self._total = max(0.0, float(restored.native_value))
            except (TypeError, ValueError):
                self._total = 0.0

        state = self.coordinator.data
        self._previous_active = bool(state and state.power_on)
        self._previous_power = state.working_power if state else None
        self._last_tick = monotonic()

    @property
    def native_value(self) -> float:
        """Return the restored cumulative value."""
        return round(self._total, 3)

    @property
    def extra_state_attributes(self) -> dict[str, int | float | None] | None:
        """Expose inputs used by the pellet estimate."""
        if self._kind != "pellets":
            return None
        state = self.coordinator.data
        power = state.working_power if state else None
        return {
            "power_level": power,
            "estimated_rate_kg_per_hour": pellet_rate(
                power,
                self._kg_per_hour_min,
                self._kg_per_hour_max,
            ),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Accumulate the interval represented by the previously observed state."""
        now = monotonic()
        elapsed = capped_elapsed_seconds(
            self._last_tick,
            now,
            self._scan_interval_seconds,
        )
        if self._previous_active:
            if self._kind == "hours":
                self._total += elapsed / 3600
            else:
                self._total += elapsed / 3600 * pellet_rate(
                    self._previous_power,
                    self._kg_per_hour_min,
                    self._kg_per_hour_max,
                )

        state = self.coordinator.data
        self._previous_active = bool(state and state.power_on)
        self._previous_power = state.working_power if state else None
        self._last_tick = now
        super()._handle_coordinator_update()
