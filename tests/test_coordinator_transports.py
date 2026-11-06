"""Coordinator behaviour that depends on the transport (cloud vs relay)."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
from datetime import timedelta
from types import SimpleNamespace

import pytest


class RelayLikeClient:
    """Transport double that measures real power, like the relay client."""

    transport_name = "the Duepi relay"
    reports_real_power = True

    def __init__(self, states: list[object] | None = None) -> None:
        self._states = list(states or [])
        self.turn_on_calls: list[dict[str, object]] = []
        self.set_power_calls: list[int] = []
        self.error: Exception | None = None

    async def async_get_stove_state(self) -> object:
        if self.error is not None:
            raise self.error
        return self._states.pop(0)

    async def async_turn_on(
        self, *, power: int, temperature: int, ignite: bool = False
    ) -> None:
        self.turn_on_calls.append(
            {"power": power, "temperature": temperature, "ignite": ignite}
        )

    async def async_set_power(self, power: int, current_state: object = None) -> None:
        self.set_power_calls.append(power)


class CloudLikeClient(RelayLikeClient):
    transport_name = "dpremoteiot.com"
    reports_real_power = False


def _entry(scheduled: list[object]) -> SimpleNamespace:
    def _schedule(_hass: object, coro: object, *, name: str) -> None:
        scheduled.append(coro)

    return SimpleNamespace(
        data={"device_id": "relay_00000000"},
        options={},
        async_create_background_task=_schedule,
    )


def _coordinator(duepi: SimpleNamespace, client: object, scheduled: list[object]):
    return duepi.coordinator.DuepiCoordinator(
        object(),
        _entry(scheduled),
        client,
        timedelta(seconds=30),
        default_power=2,
        default_temperature=21,
        eco_power=1,
        eco_temperature=18,
    )


def _state(duepi: SimpleNamespace, *, real_power: int, power_on: bool = True, status: str = "Heating"):
    return duepi.api.DuepiStoveState(
        power_on=power_on,
        status_text=status,
        room_temperature=20.0,
        working_power=real_power or None,
        set_temperature=21 if power_on else None,
        raw_online=True,
        online=True,
        real_power=real_power,
    )


def _poll(coordinator: object) -> object:
    state = asyncio.run(coordinator._async_update_data())
    coordinator.async_set_updated_data(state)
    return state


def _close(scheduled: list[object]) -> None:
    for coro in scheduled:
        coro.close()


def test_cloud_client_conforms_to_the_protocol(duepi_test_modules: SimpleNamespace) -> None:
    """protects=the cloud client keeps every DuepiClient member and parameter;
    fails_when=a member or parameter is added to the protocol but not the client;
    no_claim=the relay client (tests/test_relay.py)."""
    api = duepi_test_modules.api
    protocol = api.DuepiClient
    client = api.DuepiCloudClient
    members = [
        name
        for name in vars(protocol)
        if not name.startswith("_") and name not in {"transport_name", "reports_real_power"}
    ]
    assert set(members) >= {
        "device_id",
        "async_get_stove_state",
        "async_turn_on",
        "async_turn_off",
        "async_set_power",
        "async_set_temperature",
        "async_close",
    }
    for name in ("transport_name", "reports_real_power"):
        assert name in protocol.__annotations__
        assert hasattr(client, name)
    for name in members:
        expected = getattr(protocol, name)
        actual = getattr(client, name)
        if isinstance(expected, property):
            assert isinstance(actual, property), name
            continue
        assert list(inspect.signature(actual).parameters) == list(
            inspect.signature(expected).parameters
        ), name


def _relay_state(duepi: SimpleNamespace, *, setting: int, real_power: int):
    state = _state(duepi, real_power=real_power)
    return dataclasses.replace(state, working_power=setting, power_setting=setting)


def test_relay_controls_show_the_stove_setting_while_it_modulates(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """protects=power control and presets show the stove's own setting (RD3000,
    D13), including changes made on the panel, while real power modulates;
    fails_when=the coordinator overrides working_power with real or remembered
    power; no_claim=register meanings (covered by test_relay)."""
    duepi = duepi_test_modules
    scheduled: list[object] = []
    client = RelayLikeClient(
        [
            _relay_state(duepi, setting=1, real_power=1),
            _relay_state(duepi, setting=1, real_power=3),
            _relay_state(duepi, setting=2, real_power=1),  # changed on the panel
        ]
    )
    coordinator = _coordinator(duepi, client, scheduled)

    first = _poll(coordinator)
    assert (first.working_power, first.real_power) == (1, 1)
    assert coordinator.current_preset() == "none"  # eco is P1 at 18 C, target 21
    second = _poll(coordinator)
    assert (second.working_power, second.real_power) == (1, 3)
    third = _poll(coordinator)
    assert (third.working_power, third.real_power) == (2, 1)
    _close(scheduled)


def test_stove_off_refusal_becomes_a_translated_ui_error(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """protects=users see why a power change was refused; fails_when=the raw
    transport error leaks or the translation key changes; no_claim=frontend text."""
    duepi = duepi_test_modules

    class RefusingClient(RelayLikeClient):
        async def async_set_power(self, power: int, current_state: object = None) -> None:
            raise duepi.api.DuepiStoveOffError("not heating")

        async def async_turn_on(self, **_kwargs: object) -> None:
            raise duepi.api.DuepiStartRefusedError("busy")

    coordinator = _coordinator(duepi, RefusingClient(), [])
    coordinator.async_set_updated_data(_state(duepi, real_power=0, power_on=False, status="Off"))
    exceptions = importlib.import_module("homeassistant.exceptions")
    with pytest.raises(exceptions.HomeAssistantError) as err:
        asyncio.run(coordinator.async_set_power(3))
    assert err.value.translation_key == "stove_off"
    assert err.value.translation_domain == "duepi"
    with pytest.raises(exceptions.HomeAssistantError) as err:
        asyncio.run(coordinator.async_turn_on())
    assert err.value.translation_key == "start_refused"


def test_cloud_working_power_is_left_as_reported(duepi_test_modules: SimpleNamespace) -> None:
    """protects=cloud entries keep their behaviour; fails_when=the intent override
    leaks into the cloud transport; no_claim=none beyond protects."""
    duepi = duepi_test_modules
    scheduled: list[object] = []
    client = CloudLikeClient([_state(duepi, real_power=3), _state(duepi, real_power=5)])
    coordinator = _coordinator(duepi, client, scheduled)
    assert _poll(coordinator).working_power == 3
    assert _poll(coordinator).working_power == 5
    _close(scheduled)


@pytest.mark.parametrize(
    ("client_class", "enforcement_expected"),
    [(RelayLikeClient, False), (CloudLikeClient, True)],
)
def test_turn_on_ignites_and_only_the_cloud_re_sends_power(
    duepi_test_modules: SimpleNamespace, client_class: type, enforcement_expected: bool
) -> None:
    """protects=turn_on is the igniting call; the relay skips the cloud's power
    re-send after ignition; fails_when=ignite is dropped or enforcement runs on
    the relay; no_claim=what the relay sends on the wire (T2)."""
    duepi = duepi_test_modules
    scheduled: list[object] = []
    client = client_class([_state(duepi, real_power=1, status="Heating")])
    coordinator = _coordinator(duepi, client, scheduled)
    coordinator.async_set_updated_data(_state(duepi, real_power=0, power_on=False, status="Off"))

    asyncio.run(coordinator.async_turn_on())
    _poll(coordinator)

    assert client.turn_on_calls == [{"power": 2, "temperature": 21, "ignite": True}]
    assert bool(scheduled) is enforcement_expected
    _close(scheduled)


@pytest.mark.parametrize("client_class", [RelayLikeClient, CloudLikeClient])
def test_preset_on_a_running_stove_never_asks_to_ignite(
    duepi_test_modules: SimpleNamespace, client_class: type
) -> None:
    """protects=only an explicit turn-on may start the stove (R1); fails_when=the
    preset path passes ignite=True; no_claim=the relay-side refusal (T2)."""
    duepi = duepi_test_modules
    client = client_class()
    coordinator = _coordinator(duepi, client, [])
    coordinator.async_set_updated_data(_state(duepi, real_power=3))

    asyncio.run(coordinator.async_set_preset("eco"))

    assert client.turn_on_calls == [{"power": 1, "temperature": 18, "ignite": False}]


def test_update_failure_names_the_relay(duepi_test_modules: SimpleNamespace) -> None:
    duepi = duepi_test_modules
    client = RelayLikeClient()
    client.error = duepi.api.DuepiTransportError("no reply")
    coordinator = _coordinator(duepi, client, [])
    with pytest.raises(Exception, match="Cannot connect to the Duepi relay: no reply"):
        asyncio.run(coordinator._async_update_data())
