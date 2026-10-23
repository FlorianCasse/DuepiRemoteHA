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


class SequencedPollingClient(CommandClient):
    """Return queued stove states and record power commands."""

    def __init__(self, states: list[object]) -> None:
        super().__init__()
        self._states = states
        self.set_power_calls: list[int] = []
        self.set_power_states: list[object] = []
        self.set_power_error: Exception | None = None

    async def async_get_stove_state(self) -> object:
        return self._states.pop(0)

    async def async_turn_off(self) -> None:
        return None

    async def async_set_power(self, power: int, current_state: object = None) -> None:
        self.set_power_calls.append(power)
        self.set_power_states.append(current_state)
        if self.set_power_error is not None:
            raise self.set_power_error


def _polling_state(
    duepi: SimpleNamespace,
    status: str | None,
    *,
    set_temperature: int | None = 25,
    power_on: bool = True,
) -> object:
    """Build a polled stove state whose cloud-reported power echoes 1."""
    return duepi.api.DuepiStoveState(
        power_on=power_on,
        status_text=status,
        room_temperature=20.0,
        working_power=1,
        set_temperature=set_temperature,
        raw_online=True,
        online=True,
    )


def _polling_coordinator(
    duepi: SimpleNamespace, statuses: list[object]
) -> tuple[object, SequencedPollingClient, list[object]]:
    """Build an unconfigured coordinator that records scheduled enforcement.

    Each entry is a status string (or None) for a powered-on stove, or a
    prebuilt stove state.
    """
    scheduled: list[object] = []
    entry = _entry()
    entry.options = {}
    entry.async_create_background_task = (
        lambda hass, coro, *, name: scheduled.append(coro)
    )
    client = SequencedPollingClient(
        [
            s if isinstance(s, duepi.api.DuepiStoveState) else _polling_state(duepi, s)
            for s in statuses
        ]
    )
    coordinator = duepi.coordinator.DuepiCoordinator(
        object(), entry, client, timedelta(seconds=30)
    )
    coordinator.refreshes = 0

    async def request_refresh() -> None:
        coordinator.refreshes += 1

    coordinator.async_request_refresh = request_refresh
    return coordinator, client, scheduled


def _poll(coordinator: object, times: int) -> None:
    for _ in range(times):
        coordinator.async_set_updated_data(
            asyncio.run(coordinator._async_update_data())
        )


def test_turn_on_without_options_starts_at_power_one(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Unconfigured entries start the stove at the lowest power level."""
    coordinator, client, _scheduled = _polling_coordinator(duepi_test_modules, [])

    asyncio.run(coordinator.async_turn_on())

    assert client.turn_on_calls == [(1, 25)]


def test_turn_on_enforces_power_once_on_first_heating_despite_cloud_echo(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """The cloud echoes settedPower, so heating re-sends it once per turn-on."""
    coordinator, client, scheduled = _polling_coordinator(
        duepi_test_modules,
        ["Igniction", "Heating", "Cleaning Brazier", None, "Heating"],
    )

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 5)

    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert client.set_power_calls == [1]
    assert coordinator.refreshes == 1


def test_heating_without_ha_turn_on_does_not_enforce(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A stove started from its panel or app keeps the power it chose."""
    coordinator, client, scheduled = _polling_coordinator(
        duepi_test_modules, ["Igniction", "Heating"]
    )
    coordinator._desired_power = 3

    _poll(coordinator, 2)

    assert scheduled == []
    assert client.set_power_calls == []


def test_turn_off_before_heating_cancels_enforcement(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Turning off during ignition must not push a power command later."""
    coordinator, _client, scheduled = _polling_coordinator(
        duepi_test_modules, ["Heating"]
    )

    asyncio.run(coordinator.async_turn_on())
    asyncio.run(coordinator.async_turn_off())
    _poll(coordinator, 1)

    assert scheduled == []


def test_enforcement_sends_latest_desired_power(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A power change made after scheduling is not overwritten by a stale value."""
    coordinator, client, scheduled = _polling_coordinator(
        duepi_test_modules, ["Heating"]
    )

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 1)
    coordinator._desired_power = 4
    asyncio.run(scheduled[0])

    assert client.set_power_calls == [4]


def test_failed_enforcement_retries_on_next_heating_poll(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A failed cloud command keeps enforcement pending instead of dropping it."""
    coordinator, client, scheduled = _polling_coordinator(
        duepi_test_modules, ["Heating", "Heating", "Heating"]
    )
    client.set_power_error = duepi_test_modules.api.DuepiServerError("boom")

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 1)
    asyncio.run(scheduled[0])
    client.set_power_error = None
    _poll(coordinator, 1)
    asyncio.run(scheduled[1])
    _poll(coordinator, 1)

    assert client.set_power_calls == [1, 1]
    assert len(scheduled) == 2
    assert coordinator.refreshes == 1


def test_turn_off_after_scheduling_skips_enforcement_command(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A scheduled enforcement must never send active=1 after a turn-off."""
    coordinator, client, scheduled = _polling_coordinator(
        duepi_test_modules, ["Heating"]
    )

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 1)
    asyncio.run(coordinator.async_turn_off())
    asyncio.run(scheduled[0])

    assert client.set_power_calls == []


def test_turn_off_during_failing_enforcement_does_not_re_arm(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A send that fails after a turn-off must not schedule another active=1."""
    duepi = duepi_test_modules
    coordinator, client, scheduled = _polling_coordinator(duepi, ["Heating", "Heating"])

    async def fail_after_turn_off(power: int, current_state: object = None) -> None:
        client.set_power_calls.append(power)
        coordinator._enforce_power_pending = False
        coordinator._turn_off_generation += 1
        raise duepi.api.DuepiServerError("boom")

    client.async_set_power = fail_after_turn_off

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 1)
    asyncio.run(scheduled[0])
    _poll(coordinator, 1)

    assert client.set_power_calls == [1]
    assert len(scheduled) == 1


def test_stove_off_before_heating_drops_enforcement(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """An alarm or panel turn-off during ignition disarms enforcement."""
    duepi = duepi_test_modules
    coordinator, _client, scheduled = _polling_coordinator(
        duepi,
        [
            "Igniction",
            _polling_state(duepi, "Stove Off", power_on=False),
            "Heating",
        ],
    )

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 3)

    assert scheduled == []


def test_lagging_off_report_after_turn_on_keeps_enforcement(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """The cloud may still report OFF right after turn-on; that is not a disarm."""
    duepi = duepi_test_modules
    coordinator, _client, scheduled = _polling_coordinator(
        duepi,
        [_polling_state(duepi, "Heating", power_on=False), "Heating"],
    )

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 2)

    assert len(scheduled) == 1
    scheduled[0].close()


def test_enforcement_is_built_from_the_heating_poll(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """The command uses the poll that saw heating, not possibly stale self.data."""
    duepi = duepi_test_modules
    coordinator, client, scheduled = _polling_coordinator(duepi, ["Heating"])

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 1)
    coordinator.async_set_updated_data(_polling_state(duepi, "Stove Off", power_on=False))
    asyncio.run(scheduled[0])

    assert client.set_power_states[0].power_on is True


def test_enforcement_skips_when_set_temperature_is_unknown(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """An unparsed target temperature must not be replaced by the 25°C fallback."""
    duepi = duepi_test_modules
    coordinator, client, scheduled = _polling_coordinator(
        duepi, [_polling_state(duepi, "Heating", set_temperature=None)]
    )

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator, 1)
    asyncio.run(scheduled[0])

    assert client.set_power_calls == []


def test_enforcement_gives_up_after_max_attempts(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Persistent cloud failures stop retrying instead of posting every poll."""
    duepi = duepi_test_modules
    attempts = duepi.coordinator.MAX_ENFORCE_POWER_ATTEMPTS
    coordinator, client, scheduled = _polling_coordinator(
        duepi, ["Heating"] * (attempts + 1)
    )
    client.set_power_error = duepi.api.DuepiConnectionError("rejected")

    asyncio.run(coordinator.async_turn_on())
    for index in range(attempts + 1):
        _poll(coordinator, 1)
        if index < len(scheduled):
            asyncio.run(scheduled[index])

    assert len(client.set_power_calls) == attempts
    assert len(scheduled) == attempts


def test_default_comfort_and_eco_presets_are_distinguished_by_temperature(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """With default power 1 for both presets, temperature alone separates them."""
    coordinator, _client, _scheduled = _polling_coordinator(duepi_test_modules, [])
    assert coordinator.preset_targets("comfort") == (1, 25)
    assert coordinator.preset_targets("eco") == (1, 18)

    expected = {25: "comfort", 18: "eco", 22: "none"}
    for temperature, preset in expected.items():
        coordinator.async_set_updated_data(
            _polling_state(duepi_test_modules, "Heating", set_temperature=temperature)
        )
        assert coordinator.current_preset() == preset


@pytest.mark.parametrize(
    ("options", "expected_power", "expected_temperature"),
    [
        ({"scan_interval": 30, "default_power": 4, "default_temperature": 27}, 4, 27),
        ({}, 1, 25),
    ],
)
def test_setup_uses_ha_managed_session_with_isolated_cookie_jar(
    monkeypatch: pytest.MonkeyPatch,
    duepi_test_modules: SimpleNamespace,
    options: dict[str, int],
    expected_power: int,
    expected_temperature: int,
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
        options=options,
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
        "default_power": expected_power,
        "default_temperature": expected_temperature,
        "eco_power": 1,
        "eco_temperature": 18,
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
            alarm="A01",
        ),
        raw_online=False,
        filtered_online=True,
        disconnect_elapsed_seconds=0,
        last_update_success=True,
        last_successful_update_time="2026-07-13T10:00:00+00:00",
        last_seen="2026-07-13T09:59:30+00:00",
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
    assert result["last_seen"] == "2026-07-13T09:59:30+00:00"
    assert result["state"]["alarm"] == "A01"
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
