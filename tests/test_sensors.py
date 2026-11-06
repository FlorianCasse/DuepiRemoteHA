"""Sensor platform tests: relay-only sensors and the pellet estimate input."""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType, SimpleNamespace

import pytest

CODE = "0123456789"
RELAY_KEYS = {"real_power", "power_setting", "fumes_temperature", "phase", "error_code"}


def _install_sensor_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Home Assistant sensor surface when Home Assistant is absent."""
    try:
        importlib.import_module("homeassistant.components.sensor")
        return
    except ModuleNotFoundError:
        pass

    class SensorDeviceClass(StrEnum):
        TEMPERATURE = "temperature"
        DURATION = "duration"
        WEIGHT = "weight"
        TIMESTAMP = "timestamp"

    class SensorStateClass(StrEnum):
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        translation_key: str | None = None
        icon: str | None = None
        device_class: object = None
        state_class: object = None
        native_unit_of_measurement: object = None
        entity_category: object = None
        entity_registry_enabled_default: bool = True

    class SensorEntity:
        pass

    class RestoreSensor(SensorEntity):
        pass

    class CoordinatorEntity:
        def __init__(self, coordinator: object) -> None:
            self.coordinator = coordinator

        def __class_getitem__(cls, _item: object) -> type:
            return cls

    sensor = ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    sensor.SensorEntityDescription = SensorEntityDescription
    sensor.SensorEntity = SensorEntity
    sensor.RestoreSensor = RestoreSensor
    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object
    const = sys.modules["homeassistant.const"]
    for name, value in {
        "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
        "UnitOfMass": SimpleNamespace(KILOGRAMS="kg"),
        "UnitOfTemperature": SimpleNamespace(CELSIUS="°C"),
        "UnitOfTime": SimpleNamespace(HOURS="h"),
    }.items():
        monkeypatch.setattr(const, name, value, raising=False)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.update_coordinator"],
        "CoordinatorEntity",
        CoordinatorEntity,
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_platform", entity_platform)
    device_registry = ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)


@pytest.fixture
def sensor(duepi_test_modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    _install_sensor_stubs(monkeypatch)
    return importlib.import_module("custom_components.duepi.sensor")


def _entities(sensor, duepi: SimpleNamespace, data: dict[str, object], state: object):
    coordinator = SimpleNamespace(data=state, update_interval=None)
    entry = SimpleNamespace(data=data, options={}, runtime_data=coordinator)
    added: list[object] = []
    asyncio.run(sensor.async_setup_entry(object(), entry, added.extend))
    return {entity.entity_description.key: entity for entity in added}


def test_relay_entry_adds_the_relay_sensors(
    sensor, duepi_test_modules: SimpleNamespace
) -> None:
    """protects=relay users get real power, fumes, phase and error code (#15);
    fails_when=the relay sensors are not created or read the wrong field;
    no_claim=Home Assistant entity registry behaviour (stubbed platform)."""
    relay = duepi_test_modules.relay
    registers = {
        "RD9000": 7, "RD3000": 5, "RD4000": 4, "RD1000": 215, "RD0000": 142, "RDA000": 5,
    }
    state = relay.state_from_registers(registers)
    device_id = relay.relay_device_id(CODE)
    entities = _entities(
        sensor,
        duepi_test_modules,
        {"transport": "relay", "device_id": device_id, "device_code": CODE},
        state,
    )
    assert RELAY_KEYS <= set(entities)
    values = {key: entities[key].native_value for key in RELAY_KEYS}
    assert values == {
        "real_power": 4, "power_setting": 5, "fumes_temperature": 142.0, "phase": 7, "error_code": 5,
    }
    assert all(CODE not in entity._attr_unique_id for entity in entities.values())
    assert entities["real_power"]._attr_unique_id == f"{device_id}_real_power"


def test_cloud_entry_has_no_relay_sensors(sensor, duepi_test_modules: SimpleNamespace) -> None:
    """protects=cloud users do not get permanently unknown sensors; fails_when=relay
    sensors are added for entries without the relay transport; no_claim=none."""
    state = duepi_test_modules.api.DuepiStoveState(
        power_on=True,
        status_text="Heating",
        room_temperature=20.0,
        working_power=3,
        set_temperature=21,
        raw_online=True,
        online=True,
    )
    entities = _entities(sensor, duepi_test_modules, {"device_id": "cloud-1"}, state)
    assert not RELAY_KEYS & set(entities)
    assert "room_temperature" in entities


def test_pellet_estimate_uses_the_measured_power_when_available(
    sensor, duepi_test_modules: SimpleNamespace
) -> None:
    """protects=pellet use follows what the stove burns, not what HA requested
    (relay keeps working_power as intent, R7); fails_when=the estimate reads
    working_power on relay; no_claim=pellet rate accuracy."""
    api = duepi_test_modules.api
    base = dict(
        power_on=True, status_text="Heating", room_temperature=20.0,
        set_temperature=21, raw_online=True, online=True,
    )
    relay_state = api.DuepiStoveState(working_power=1, real_power=4, **base)
    cloud_state = api.DuepiStoveState(working_power=3, **base)
    assert sensor.burning_power(relay_state) == 4
    assert sensor.burning_power(cloud_state) == 3
    assert sensor.burning_power(None) is None
