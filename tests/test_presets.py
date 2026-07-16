"""Coordinator tests for Duepi climate presets and problem detection."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest


class CommandClient:
    """Record combined turn-on commands and optionally fail them."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[int, int]] = []
        self.fail = fail

    async def async_turn_on(self, *, power: int, temperature: int) -> None:
        self.calls.append((power, temperature))
        if self.fail:
            raise RuntimeError("command failed")


def _coordinator(
    duepi: SimpleNamespace,
    client: CommandClient | None = None,
    *,
    default_power: int = 5,
    default_temperature: int = 25,
    eco_power: int = 1,
    eco_temperature: int = 18,
) -> object:
    """Build a coordinator with deterministic preset options."""
    return duepi.coordinator.DuepiCoordinator(
        object(),
        SimpleNamespace(async_create_background_task=lambda *_args, **_kwargs: None),
        client or CommandClient(),
        timedelta(seconds=30),
        default_power=default_power,
        default_temperature=default_temperature,
        eco_power=eco_power,
        eco_temperature=eco_temperature,
    )


def _state(
    duepi: SimpleNamespace,
    *,
    power_on: bool,
    power: int = 3,
    temperature: int = 22,
    alarm: str | None = None,
    status: str | None = "Heating",
) -> object:
    """Build a complete stove state."""
    return duepi.api.DuepiStoveState(
        power_on=power_on,
        status_text=status,
        room_temperature=20.0,
        working_power=power,
        set_temperature=temperature,
        raw_online=True,
        online=True,
        alarm=alarm,
    )


def test_preset_targets_and_current_match(duepi_test_modules: SimpleNamespace) -> None:
    """Eco, comfort, and unmatched states map to stable preset names."""
    coordinator = _coordinator(duepi_test_modules)
    assert coordinator.preset_targets("eco") == (1, 18)
    assert coordinator.preset_targets("comfort") == (5, 25)
    assert coordinator.preset_targets("none") is None

    coordinator.async_set_updated_data(
        _state(duepi_test_modules, power_on=True, power=1, temperature=18)
    )
    assert coordinator.current_preset() == "eco"
    coordinator.async_set_updated_data(
        _state(duepi_test_modules, power_on=True, power=5, temperature=25)
    )
    assert coordinator.current_preset() == "comfort"
    coordinator.async_set_updated_data(_state(duepi_test_modules, power_on=True))
    assert coordinator.current_preset() == "none"

    with pytest.raises(ValueError, match="Unsupported preset"):
        coordinator.preset_targets("holiday")


def test_eco_wins_when_preset_targets_collide(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A target matching both configured pairs is presented as eco."""
    coordinator = _coordinator(
        duepi_test_modules,
        default_power=1,
        default_temperature=18,
    )
    coordinator.async_set_updated_data(
        _state(duepi_test_modules, power_on=True, power=1, temperature=18)
    )
    assert coordinator.current_preset() == "eco"


def test_active_preset_uses_one_combined_command_and_preserves_alarm(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Optimistic preset updates preserve every unrelated state field."""
    client = CommandClient()
    coordinator = _coordinator(duepi_test_modules, client)
    coordinator.async_set_updated_data(
        _state(duepi_test_modules, power_on=True, alarm="A01")
    )

    asyncio.run(coordinator.async_set_preset("eco"))

    assert client.calls == [(1, 18)]
    assert coordinator.data.working_power == 1
    assert coordinator.data.set_temperature == 18
    assert coordinator.data.alarm == "A01"


def test_off_preset_is_consumed_only_after_successful_turn_on(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A queued preset survives a failed command and is one-shot on success."""
    failing_client = CommandClient(fail=True)
    coordinator = _coordinator(duepi_test_modules, failing_client)
    coordinator.async_set_updated_data(_state(duepi_test_modules, power_on=False))
    asyncio.run(coordinator.async_set_preset("comfort"))
    assert coordinator.current_preset() == "comfort"

    with pytest.raises(RuntimeError, match="command failed"):
        asyncio.run(coordinator.async_turn_on())
    assert coordinator.current_preset() == "comfort"

    succeeding_client = CommandClient()
    coordinator.client = succeeding_client
    asyncio.run(coordinator.async_turn_on())
    assert succeeding_client.calls == [(5, 25)]
    assert coordinator._pending_preset is None


def test_none_clears_a_queued_preset(duepi_test_modules: SimpleNamespace) -> None:
    """The explicit none mode cancels pending preset behavior without a command."""
    client = CommandClient()
    coordinator = _coordinator(duepi_test_modules, client)
    coordinator.async_set_updated_data(_state(duepi_test_modules, power_on=False))

    asyncio.run(coordinator.async_set_preset("eco"))
    asyncio.run(coordinator.async_set_preset("none"))

    assert coordinator._pending_preset is None
    assert client.calls == []


@pytest.mark.parametrize(
    ("alarm", "status", "expected"),
    [
        ("A01", "Heating", True),
        (None, "Error: fan", True),
        (None, "ALARM active", True),
        (None, "Heating", False),
    ],
)
def test_problem_detection_uses_alarm_and_status_fallback(
    duepi_test_modules: SimpleNamespace,
    alarm: str | None,
    status: str,
    expected: bool,
) -> None:
    """Problem state remains useful when speculative alarm fields are absent."""
    state = _state(
        duepi_test_modules,
        power_on=True,
        alarm=alarm,
        status=status,
    )
    assert duepi_test_modules.coordinator.state_has_problem(state) is expected
    assert duepi_test_modules.coordinator.state_has_problem(None) is None
