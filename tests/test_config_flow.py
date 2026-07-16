"""Config-flow tests for automatic Duepi device discovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _flow_with_client(
    duepi_test_modules: SimpleNamespace,
    monkeypatch: object,
    *,
    devices: list[object],
    state_error: Exception | None = None,
    list_error: Exception | None = None,
) -> tuple[object, type]:
    """Create a flow backed by a recording cloud client."""
    config_flow = duepi_test_modules.config_flow

    class StubClient:
        instance: "StubClient"

        def __init__(self, *_args: object) -> None:
            type(self).instance = self
            self.login_calls = 0
            self.state_calls = 0
            self.selected: tuple[str, str | None] | None = None

        async def async_login(self) -> bool:
            self.login_calls += 1
            return True

        async def async_list_devices(self) -> list[object]:
            if list_error is not None:
                raise list_error
            return devices

        def select_device(self, device_id: str, api_id: str | None = None) -> None:
            self.selected = (device_id, api_id)

        async def async_get_stove_state(self) -> object:
            self.state_calls += 1
            if state_error is not None:
                raise state_error
            return object()

    monkeypatch.setattr(config_flow, "DuepiCloudClient", StubClient)
    monkeypatch.setattr(
        config_flow,
        "async_create_clientsession",
        lambda *_args, **_kwargs: object(),
    )
    return config_flow.DuepiConfigFlow(), StubClient


def test_user_discovers_devices_then_creates_selected_entry(
    monkeypatch: object, duepi_test_modules: SimpleNamespace
) -> None:
    """Selection reuses the authenticated client and stable entry data shape."""
    api = duepi_test_modules.api
    flow, client_type = _flow_with_client(
        duepi_test_modules,
        monkeypatch,
        devices=[
            api.DuepiDeviceSummary("stove-one", "api-one", "Living room"),
            api.DuepiDeviceSummary("stove-two", "api-two", None),
        ],
    )

    first = asyncio.run(
        flow.async_step_user({"email": "person@example.test", "password": "secret"})
    )
    result = asyncio.run(flow.async_step_select_device({"device_id": "stove-one"}))

    assert first["type"] == "form"
    assert first["step_id"] == "select_device"
    assert result == {
        "type": "create_entry",
        "title": "Living room",
        "data": {
            "email": "person@example.test",
            "password": "secret",
            "device_id": "stove-one",
        },
    }
    assert flow._unique_id == "stove-one"
    assert client_type.instance.login_calls == 1
    assert client_type.instance.state_calls == 1
    assert client_type.instance.selected == ("stove-one", "api-one")


def test_empty_discovery_uses_manual_device_fallback(
    monkeypatch: object, duepi_test_modules: SimpleNamespace
) -> None:
    """Accounts without parseable summaries can still use their known identifier."""
    flow, client_type = _flow_with_client(
        duepi_test_modules,
        monkeypatch,
        devices=[],
    )

    first = asyncio.run(
        flow.async_step_user({"email": "person@example.test", "password": "secret"})
    )
    result = asyncio.run(flow.async_step_manual_device({"device_id": "manual-id"}))

    assert first["step_id"] == "manual_device"
    assert result["type"] == "create_entry"
    assert result["data"]["device_id"] == "manual-id"
    assert client_type.instance.selected == ("manual-id", None)
    assert client_type.instance.login_calls == 1


def test_single_device_still_uses_explicit_selection(
    monkeypatch: object, duepi_test_modules: SimpleNamespace
) -> None:
    """A one-device account receives the same clear selection screen."""
    api = duepi_test_modules.api
    flow, _client_type = _flow_with_client(
        duepi_test_modules,
        monkeypatch,
        devices=[api.DuepiDeviceSummary("only-stove", "api-id", None)],
    )

    result = asyncio.run(
        flow.async_step_user({"email": "person@example.test", "password": "secret"})
    )

    assert result["step_id"] == "select_device"


def test_discovery_transport_error_remains_on_credentials_step(
    monkeypatch: object, duepi_test_modules: SimpleNamespace
) -> None:
    """A cloud outage is not misrepresented as an unknown device."""
    api = duepi_test_modules.api
    flow, _client_type = _flow_with_client(
        duepi_test_modules,
        monkeypatch,
        devices=[],
        list_error=api.DuepiTransportError("offline"),
    )

    result = asyncio.run(
        flow.async_step_user({"email": "person@example.test", "password": "secret"})
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


def test_manual_device_maps_parse_failure(
    monkeypatch: object, duepi_test_modules: SimpleNamespace
) -> None:
    """The manual fallback keeps the existing invalid-device error."""
    api = duepi_test_modules.api
    flow, _client_type = _flow_with_client(
        duepi_test_modules,
        monkeypatch,
        devices=[],
        state_error=api.DuepiParseError("missing"),
    )

    asyncio.run(
        flow.async_step_user({"email": "person@example.test", "password": "secret"})
    )
    result = asyncio.run(flow.async_step_manual_device({"device_id": "missing"}))

    assert result["step_id"] == "manual_device"
    assert result["errors"] == {"base": "invalid_device"}
