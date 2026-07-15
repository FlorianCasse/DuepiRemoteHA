"""Regression tests for Duepi integration lifecycle and diagnostics."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


class CommandClient:
    """Record the turn-on command sent by the coordinator."""

    def __init__(self) -> None:
        self.turn_on_calls: list[tuple[int, int]] = []

    async def async_turn_on(self, *, power: int, temperature: int) -> None:
        self.turn_on_calls.append((power, temperature))


class PollingClient:
    """Return a single state from a successful coordinator poll."""

    def __init__(self, state: object) -> None:
        self._state = state

    async def async_get_stove_state(self) -> object:
        return self._state


def _entry() -> SimpleNamespace:
    """Create a config entry double with configuration and task scheduling."""
    return SimpleNamespace(
        data={"device_id": "device-secret", "email": "person@example.test", "password": "password-secret"},
        options={"default_power": 4, "default_temperature": 27},
        background_tasks=[],
        async_create_background_task=lambda hass, coro, *, name: None,
    )


def test_turn_on_uses_configured_defaults_even_when_old_state_exists(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Configured start settings must override stale reported settings."""
    entry = _entry()
    client = CommandClient()
    coordinator = duepi_test_modules.coordinator.DuepiCoordinator(
        object(),
        entry,
        client,
        timedelta(seconds=30),
        default_power=entry.options["default_power"],
        default_temperature=entry.options["default_temperature"],
    )
    coordinator.async_set_updated_data(
        duepi_test_modules.api.DuepiStoveState(
            power_on=False,
            status_text="Off",
            room_temperature=18.0,
            working_power=1,
            set_temperature=19,
            raw_online=True,
            online=True,
        )
    )

    asyncio.run(coordinator.async_turn_on())

    assert client.turn_on_calls == [(4, 27)]
    assert coordinator.config_entry is entry


def test_setup_uses_ha_managed_session_with_isolated_cookie_jar(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """Cookie-isolated sessions are owned and closed by Home Assistant."""
    duepi_init = importlib.import_module("custom_components.duepi.__init__")
    created: dict[str, object] = {}

    class CookieJar:
        pass

    class FakeSession:
        closed = False

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            created["client_session"] = kwargs["session"]

        async def async_login(self) -> bool:
            return True

        async def async_close(self) -> None:
            raise AssertionError("HA-owned sessions must not be closed by the client")

    class FakeCoordinator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["coordinator_args"] = args
            created["coordinator_kwargs"] = kwargs
            self.client = args[2]

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def async_cancel_disconnect_grace(self) -> None:
            return None

    def create_session(hass: object, **kwargs: object) -> FakeSession:
        created["hass"] = hass
        created["session_kwargs"] = kwargs
        return FakeSession()

    monkeypatch.setattr(duepi_init.aiohttp, "CookieJar", CookieJar)
    monkeypatch.setattr(duepi_init, "async_create_clientsession", create_session)
    monkeypatch.setattr(duepi_init, "DuepiCloudClient", FakeClient)
    monkeypatch.setattr(duepi_init, "DuepiCoordinator", FakeCoordinator)
    entry = SimpleNamespace(
        data={"email": "person@example.test", "password": "password-secret", "device_id": "device-secret"},
        options={"scan_interval": 30, "default_power": 4, "default_temperature": 27},
        async_on_unload=lambda _callback: None,
        add_update_listener=lambda _listener: object(),
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=lambda _entry, _platforms: _return_none()
        )
    )

    assert asyncio.run(duepi_init.async_setup_entry(hass, entry)) is True

    assert created["hass"] is hass
    assert isinstance(created["session_kwargs"]["cookie_jar"], CookieJar)
    assert created["client_session"].closed is False
    assert created["coordinator_args"][1] is entry
    assert created["coordinator_kwargs"] == {
        "default_power": 4,
        "default_temperature": 27,
        "update_interval": timedelta(seconds=30),
    }


def test_diagnostics_redacts_all_identifiers_and_exposes_connectivity_timing(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Diagnostics preserve useful status without exposing account or device data."""
    diagnostics = importlib.import_module("custom_components.duepi.diagnostics")
    entry = _entry()
    entry.runtime_data = SimpleNamespace(
        data=duepi_test_modules.api.DuepiStoveState(
            power_on=True,
            status_text="Heating",
            room_temperature=21.5,
            working_power=4,
            set_temperature=27,
            raw_online=False,
            online=True,
        ),
        raw_online=False,
        filtered_online=True,
        disconnect_elapsed_seconds=0,
        last_update_success=True,
        last_successful_update_time="2026-07-13T10:00:00+00:00",
        last_exception=RuntimeError("email=person@example.test token=secret"),
    )

    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(object(), entry))

    serialized = repr(result)
    assert result["connectivity"] == {
        "raw_online": False,
        "filtered_online": True,
        "disconnect_elapsed_seconds": 0,
        "grace_period_seconds": 120,
    }
    assert result["last_successful_update"] == "2026-07-13T10:00:00+00:00"
    assert result["last_error"] == "RuntimeError"
    assert "device-secret" not in serialized
    assert "person@example.test" not in serialized
    assert "password-secret" not in serialized
    assert "**REDACTED**" in serialized


def test_diagnostics_reports_timestamp_only_after_successful_coordinator_poll(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A successful poll supplies diagnostics with a UTC timestamp."""
    diagnostics = importlib.import_module("custom_components.duepi.diagnostics")
    entry = _entry()
    coordinator = duepi_test_modules.coordinator.DuepiCoordinator(
        object(),
        entry,
        PollingClient(
            duepi_test_modules.api.DuepiStoveState(
                power_on=True,
                status_text="Heating",
                room_temperature=21.5,
                working_power=4,
                set_temperature=27,
                raw_online=True,
                online=True,
            )
        ),
        timedelta(seconds=30),
    )
    entry.runtime_data = coordinator
    coordinator.last_update_success = False

    before_poll = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(object(), entry)
    )

    fixed_time = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)

    class FixedDateTime:
        @classmethod
        def now(cls, tz: timezone) -> datetime:
            assert tz is timezone.utc
            return fixed_time

    monkeypatch.setattr(duepi_test_modules.coordinator, "datetime", FixedDateTime)
    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))
    coordinator.last_update_success = True

    after_poll = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(object(), entry)
    )

    assert before_poll["last_successful_update"] is None
    assert after_poll["last_successful_update"] == fixed_time.isoformat()


async def _return_none() -> None:
    """Return no result for an async forwarding stub."""
