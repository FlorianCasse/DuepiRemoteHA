"""Isolated test doubles for optional Home Assistant dependencies."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class _ConfigEntry:
    """Test stand-in for Home Assistant's ConfigEntry."""

    def __class_getitem__(cls, _item: object) -> type["_ConfigEntry"]:
        return cls


class _ConfigFlow:
    """Minimal ConfigFlow base that accepts Home Assistant's domain keyword."""

    def __init__(self) -> None:
        self.hass = _HomeAssistant()
        self._unique_id = None

    def __init_subclass__(cls, **_kwargs: object) -> None:
        super().__init_subclass__()

    def async_show_form(self, **kwargs: object) -> dict[str, object]:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs: object) -> dict[str, object]:
        return {"type": "create_entry", **kwargs}

    async def async_set_unique_id(self, unique_id: str) -> None:
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        return None


class _OptionsFlow:
    """Minimal OptionsFlow base for importing the configuration module."""


class _HomeAssistant:
    """Test stand-in for Home Assistant."""


class _ConfigEntryAuthFailed(Exception):
    """Test stand-in for Home Assistant's authentication exception."""


class _DataUpdateCoordinator:
    """Small subset used by DuepiCoordinator in these unit tests."""

    def __init__(self, hass: object, logger: object, **kwargs: object) -> None:
        self.hass = hass
        self.logger = logger
        self.data = None

    def async_set_updated_data(self, data: object) -> None:
        self.data = data

    def __class_getitem__(cls, _item: object) -> type["_DataUpdateCoordinator"]:
        return cls


class _UpdateFailed(Exception):
    """Test stand-in for Home Assistant's update exception."""


@dataclass
class _ScheduledCall:
    delay: float
    callback: Callable[[datetime], None]
    cancelled: bool = False


class _Scheduler:
    """Capture Home Assistant delayed callbacks for deterministic unit tests."""

    def __init__(self) -> None:
        self.calls: list[_ScheduledCall] = []

    def async_call_later(
        self, _hass: object, delay: float, callback: Callable[[datetime], None]
    ) -> Callable[[], None]:
        call = _ScheduledCall(delay, callback)
        self.calls.append(call)

        def cancel() -> None:
            call.cancelled = True

        return cancel

    def fire(self, call: _ScheduledCall) -> None:
        assert not call.cancelled
        call.callback(datetime.now())


def _install_homeassistant_stubs(monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduler) -> None:
    """Install stubs only when Home Assistant is genuinely unavailable."""
    homeassistant = ModuleType("homeassistant")
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = _ConfigEntry
    config_entries.ConfigFlow = _ConfigFlow
    config_entries.OptionsFlow = _OptionsFlow
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = _HomeAssistant
    core.callback = lambda func: func
    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = _ConfigEntryAuthFailed
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    const = ModuleType("homeassistant.const")
    const.CONF_EMAIL = "email"
    const.CONF_PASSWORD = "password"
    data_entry_flow = ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    helpers = ModuleType("homeassistant.helpers")
    event = ModuleType("homeassistant.helpers.event")
    event.async_call_later = scheduler.async_call_later
    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
    update_coordinator.UpdateFailed = _UpdateFailed
    aiohttp_client = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_create_clientsession = lambda _hass, **_kwargs: object()
    components = ModuleType("homeassistant.components")
    diagnostics = ModuleType("homeassistant.components.diagnostics")

    def async_redact_data(data: dict[str, object], keys: set[str]) -> dict[str, object]:
        """Redact configured fields using Home Assistant's diagnostics marker."""
        return {
            key: "**REDACTED**" if key in keys and value is not None else value
            for key, value in data.items()
        }

    diagnostics.async_redact_data = async_redact_data

    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.const": const,
        "homeassistant.data_entry_flow": data_entry_flow,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.helpers.update_coordinator": update_coordinator,
        "homeassistant.components": components,
        "homeassistant.components.diagnostics": diagnostics,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def _install_aiohttp_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install an aiohttp double only when aiohttp is unavailable."""
    aiohttp = ModuleType("aiohttp")
    aiohttp.ClientTimeout = lambda **kwargs: kwargs
    aiohttp.ClientSession = object
    aiohttp.CookieJar = object
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)


def _install_voluptuous_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide only the schema helpers evaluated while importing config_flow."""
    voluptuous = ModuleType("voluptuous")
    voluptuous.Schema = lambda value: value
    voluptuous.Required = lambda value, **_kwargs: value
    voluptuous.Optional = lambda value, **_kwargs: value
    voluptuous.All = lambda *values: values[-1]
    voluptuous.Coerce = lambda value: value
    voluptuous.In = lambda value: value
    voluptuous.Range = lambda **_kwargs: (lambda value: value)
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous)


@pytest.fixture
def duepi_test_modules(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Load Duepi modules without changing dependency imports outside one test."""
    modules_before = set(sys.modules)
    scheduler = _Scheduler()
    try:
        importlib.import_module("homeassistant")
    except ModuleNotFoundError as err:
        if err.name != "homeassistant":
            raise
        _install_homeassistant_stubs(monkeypatch, scheduler)

    try:
        importlib.import_module("aiohttp")
    except ModuleNotFoundError as err:
        if err.name != "aiohttp":
            raise
        _install_aiohttp_stub(monkeypatch)

    try:
        importlib.import_module("voluptuous")
    except ModuleNotFoundError as err:
        if err.name != "voluptuous":
            raise
        _install_voluptuous_stub(monkeypatch)

    package_name = "custom_components.duepi"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "duepi")]
    monkeypatch.setitem(sys.modules, package_name, package)

    try:
        api = importlib.import_module(f"{package_name}.api")
        coordinator = importlib.import_module(f"{package_name}.coordinator")
        config_flow = importlib.import_module(f"{package_name}.config_flow")
        monkeypatch.setattr(coordinator, "async_call_later", scheduler.async_call_later)
        yield SimpleNamespace(
            api=api,
            coordinator=coordinator,
            config_flow=config_flow,
            scheduler=scheduler,
        )
    finally:
        for name in set(sys.modules) - modules_before:
            if name == "custom_components" or name.startswith(f"{package_name}."):
                del sys.modules[name]
