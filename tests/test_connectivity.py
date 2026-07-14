"""Regression tests for Duepi connectivity filtering."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace

import pytest


class FakeClient:
    """Return each supplied stove state in polling order."""

    def __init__(self, states: list[object]) -> None:
        self._states: Iterator[object] = iter(states)

    async def async_get_stove_state(self) -> object:
        return next(self._states)


class CloseableFakeClient(FakeClient):
    """Record that teardown cancelled the delayed callback before closing."""

    def __init__(self, states: list[object]) -> None:
        super().__init__(states)
        self.delayed_call: object | None = None
        self.closed = False

    async def async_close(self) -> None:
        self.closed = True


def config_entry() -> SimpleNamespace:
    """Return the coordinator's minimal config entry dependency."""
    return SimpleNamespace(async_create_background_task=lambda *_args, **_kwargs: None)


def stove_state(api: SimpleNamespace, raw_online: bool | None) -> object:
    """Build a minimal raw state as currently returned by the API client."""
    return api.DuepiStoveState(
        power_on=False,
        status_text=None,
        room_temperature=None,
        working_power=None,
        set_temperature=None,
        raw_online=raw_online,
        online=raw_online,
    )


def poll_states(
    monkeypatch: pytest.MonkeyPatch,
    duepi: SimpleNamespace,
    states: list[object],
    times: list[float],
) -> list[object]:
    """Poll a coordinator at the supplied monotonic times."""
    clock = {"now": 0.0}
    monkeypatch.setattr(duepi.coordinator, "monotonic", lambda: clock["now"], raising=False)
    coordinator = duepi.coordinator.DuepiCoordinator(
        object(), config_entry(), FakeClient(states), timedelta(seconds=30)
    )
    results = []
    for time in times:
        clock["now"] = time
        result = asyncio.run(coordinator._async_update_data())
        coordinator.async_set_updated_data(result)
        results.append(result)
    return results


def test_false_readings_within_grace_period_keep_stove_online(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A transient offline report after a connected report must not flap availability."""
    results = poll_states(
        monkeypatch,
        duepi_test_modules,
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, True),
        ],
        [0, 1, 30, 31],
    )

    assert [state.online for state in results] == [True, True, True, True]
    assert [state.raw_online for state in results] == [True, False, False, True]


def test_continuous_offline_for_sixty_seconds_confirms_disconnect(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A continuous raw offline period becomes published offline at 60 seconds."""
    results = poll_states(
        monkeypatch,
        duepi_test_modules,
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, False),
        ],
        [0, 1, 30, 61],
    )

    assert [state.online for state in results] == [True, True, True, False]


def test_offline_is_published_at_grace_deadline_without_another_poll(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """The delayed callback confirms a disconnect even if polling is slower than a minute."""
    results = poll_states(
        monkeypatch,
        duepi_test_modules,
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
        ],
        [0, 1],
    )

    assert [state.online for state in results] == [True, True]
    assert len(duepi_test_modules.scheduler.calls) == 1
    delayed_call = duepi_test_modules.scheduler.calls[0]
    assert delayed_call.delay == 60

    duepi_test_modules.scheduler.fire(delayed_call)

    coordinator = delayed_call.callback.__self__
    assert coordinator.data.raw_online is False
    assert coordinator.data.online is False


def test_disconnect_callback_preserves_command_state_changed_during_grace(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """The delayed callback only changes availability on the latest state."""
    clock = {"now": 0.0}
    monkeypatch.setattr(
        duepi_test_modules.coordinator,
        "monotonic",
        lambda: clock["now"],
        raising=False,
    )
    coordinator = duepi_test_modules.coordinator.DuepiCoordinator(
        object(),
        config_entry(),
        FakeClient(
            [
                stove_state(duepi_test_modules.api, True),
                stove_state(duepi_test_modules.api, False),
            ]
        ),
        timedelta(seconds=90),
    )

    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))
    clock["now"] = 1
    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))
    coordinator.async_set_updated_data(
        duepi_test_modules.api.DuepiStoveState(
            power_on=True,
            status_text="Heating",
            room_temperature=21.5,
            working_power=4,
            set_temperature=23,
            raw_online=False,
            online=True,
        )
    )

    duepi_test_modules.scheduler.fire(duepi_test_modules.scheduler.calls[0])

    assert coordinator.data == duepi_test_modules.api.DuepiStoveState(
        power_on=True,
        status_text="Heating",
        room_temperature=21.5,
        working_power=4,
        set_temperature=23,
        raw_online=False,
        online=False,
    )


def test_raw_online_cancels_a_pending_disconnect_timer(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A reconnect invalidates the delayed disconnect callback."""
    poll_states(
        monkeypatch,
        duepi_test_modules,
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, True),
        ],
        [0, 1, 2],
    )

    delayed_call = duepi_test_modules.scheduler.calls[0]
    coordinator = delayed_call.callback.__self__
    assert delayed_call.cancelled is True
    assert coordinator.disconnect_grace_started_at is None
    assert coordinator.data.online is True


def test_unload_cancels_pending_disconnect_before_closing_client(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """Teardown prevents an in-flight grace callback from outliving the entry."""
    clock = {"now": 0.0}
    monkeypatch.setattr(
        duepi_test_modules.coordinator,
        "monotonic",
        lambda: clock["now"],
        raising=False,
    )
    client = CloseableFakeClient(
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
        ]
    )
    coordinator = duepi_test_modules.coordinator.DuepiCoordinator(
        object(), config_entry(), client, timedelta(seconds=90)
    )
    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))
    clock["now"] = 1
    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))
    client.delayed_call = duepi_test_modules.scheduler.calls[0]

    duepi_init = importlib.import_module("custom_components.duepi.__init__")
    config_entries = SimpleNamespace(
        async_unload_platforms=lambda _entry, _platforms: _return_true()
    )
    hass = SimpleNamespace(config_entries=config_entries)
    entry = SimpleNamespace(runtime_data=coordinator)

    assert asyncio.run(duepi_init.async_unload_entry(hass, entry)) is True
    assert client.delayed_call.cancelled is True
    assert coordinator.disconnect_grace_started_at is None
    assert client.closed is False


async def _return_true() -> bool:
    """Return a successful platform-unload result for an isolated test."""
    return True


def test_disconnect_confirmation_is_logged_once_when_polling_resumes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A late poll cannot duplicate the timer's disconnect confirmation log."""
    caplog.set_level(logging.INFO, logger=duepi_test_modules.coordinator.__name__)
    clock = {"now": 0.0}
    monkeypatch.setattr(
        duepi_test_modules.coordinator,
        "monotonic",
        lambda: clock["now"],
        raising=False,
    )
    coordinator = duepi_test_modules.coordinator.DuepiCoordinator(
        object(),
        config_entry(),
        FakeClient(
            [
                stove_state(duepi_test_modules.api, True),
                stove_state(duepi_test_modules.api, False),
                stove_state(duepi_test_modules.api, False),
            ]
        ),
        timedelta(seconds=90),
    )

    asyncio.run(coordinator._async_update_data())
    clock["now"] = 1
    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))
    duepi_test_modules.scheduler.fire(duepi_test_modules.scheduler.calls[0])
    clock["now"] = 90
    coordinator.async_set_updated_data(asyncio.run(coordinator._async_update_data()))

    assert coordinator.data.online is False
    assert [record.message for record in caplog.records].count(
        "Stove disconnect confirmed after grace period"
    ) == 1


def test_initial_offline_state_is_published_immediately(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """Startup has no prior connected state to protect with a grace period."""
    results = poll_states(
        monkeypatch, duepi_test_modules, [stove_state(duepi_test_modules.api, False)], [0]
    )

    assert results[0].online is False


def test_raw_online_restores_connectivity_immediately(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A raw connected report ends a pending disconnect grace period immediately."""
    results = poll_states(
        monkeypatch,
        duepi_test_modules,
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
        ],
        [0, 1, 61, 62, 63],
    )

    assert [state.online for state in results] == [True, True, False, True, True]


def test_unknown_report_does_not_hide_a_later_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A reconnect remains observable when an unknown report follows a disconnect."""
    caplog.set_level(logging.INFO, logger=duepi_test_modules.coordinator.__name__)

    results = poll_states(
        monkeypatch,
        duepi_test_modules,
        [
            stove_state(duepi_test_modules.api, True),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, False),
            stove_state(duepi_test_modules.api, None),
            stove_state(duepi_test_modules.api, True),
        ],
        [0, 1, 61, 62, 63],
    )

    assert [state.online for state in results] == [True, True, False, None, True]
    assert [record.message for record in caplog.records].count(
        "Stove connectivity restored"
    ) == 1
