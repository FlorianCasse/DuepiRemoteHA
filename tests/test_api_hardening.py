"""Regression tests for Duepi cloud-client error handling and parsing."""

from __future__ import annotations

import asyncio
import html
import json
from types import SimpleNamespace

import pytest


DEVICE_ID = "stove-123"
API_ID = "0123456789abcdef01234567"


class FakeResponse:
    """Small async HTTP response double."""

    def __init__(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.reason = "test response"
        self.url = "https://dpremoteiot.com/test"

    async def text(self) -> str:
        return self._body

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeRequest:
    """Context manager returned from a fake request."""

    def __init__(self, response: FakeResponse | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSession:
    """Record requests and provide scripted responses."""

    def __init__(
        self,
        gets: list[FakeResponse | BaseException],
        posts: list[FakeResponse | BaseException] | None = None,
    ) -> None:
        self.gets = gets
        self.posts = posts or []
        self.get_calls: list[tuple[object, dict[str, object]]] = []
        self.post_calls: list[tuple[object, dict[str, object]]] = []

    def get(self, url: object, **kwargs: object) -> FakeRequest:
        self.get_calls.append((url, kwargs))
        return FakeRequest(self.gets.pop(0))

    def post(self, url: object, **kwargs: object) -> FakeRequest:
        self.post_calls.append((url, kwargs))
        return FakeRequest(self.posts.pop(0))

    async def close(self) -> None:
        return None


def dashboard(device_id: str = DEVICE_ID, *, online: object = "online") -> str:
    """Return a dashboard comment containing a valid minimal device state."""
    payload = [
        {
            "_id": API_ID,
            "univocalID": device_id,
            "deviceCurrentSettings": {
                "powerState": "ON",
                "isOnline": online,
                "status": "Heating",
                "roomTemperature": "21.5",
            },
        }
    ]
    return f"<!-- {html.escape(json.dumps(payload), quote=False)} -->"


def client(api: SimpleNamespace, session: FakeSession) -> object:
    """Build a client under test."""
    return api.DuepiCloudClient(session, "user@example.invalid", "secret", DEVICE_ID)


def test_login_server_error_is_not_invalid_credentials(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A 5xx while loading the login form is a server failure, not bad credentials."""
    api = duepi_test_modules.api
    session = FakeSession([FakeResponse(500)], [FakeResponse(401)])

    with pytest.raises(api.DuepiServerError):
        asyncio.run(client(api, session).async_login())


def test_login_unexpected_redirect_is_a_connection_failure(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Only a dashboard redirect is a successful login response."""
    api = duepi_test_modules.api
    session = FakeSession(
        [FakeResponse(200, "<form></form>")],
        [FakeResponse(302, headers={"Location": "https://unexpected.invalid/"})],
    )

    with pytest.raises(api.DuepiConnectionError):
        asyncio.run(client(api, session).async_login())


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/", True),
        ("/dashboard", True),
        ("/dashboard?welcome=1", True),
        ("/dashboard-malformed", False),
        ("/dashboard/other", False),
        ("https://dpremoteiot.com/dashboard", False),
        ("https://unexpected.invalid/dashboard", False),
    ],
)
def test_login_redirect_validation_accepts_only_root_or_dashboard(
    duepi_test_modules: SimpleNamespace, location: str, expected: bool
) -> None:
    """A successful login can only redirect to the exact local dashboard routes."""
    assert duepi_test_modules.api.DuepiCloudClient._is_dashboard_redirect(location) is expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/login", True),
        ("/login?expired=1", True),
        ("/login-malformed", False),
        ("/login/other", False),
        ("https://dpremoteiot.com/login", False),
        ("https://unexpected.invalid/login", False),
    ],
)
def test_session_expiry_redirect_validation_accepts_only_login(
    duepi_test_modules: SimpleNamespace, location: str, expected: bool
) -> None:
    """Only the exact local login route is considered a session-expiry redirect."""
    assert duepi_test_modules.api.DuepiCloudClient._is_login_redirect(location) is expected


def test_wrong_device_id_never_fabricates_an_off_state(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """A different device in a valid dashboard must be rejected."""
    api = duepi_test_modules.api

    with pytest.raises(api.DuepiParseError):
        client(api, FakeSession([]))._parse_dashboard(dashboard("other-stove"))


def test_incompatible_html_never_fabricates_an_off_state(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """HTML without a validated device/current-settings pair is not a state."""
    api = duepi_test_modules.api

    with pytest.raises(api.DuepiParseError):
        client(api, FakeSession([]))._parse_dashboard("<html>maintenance</html>")


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (" ONLINE ", True),
        ("offline", False),
        (2, None),
        ("yes", None),
        (None, None),
    ],
)
def test_is_online_is_strictly_normalized(
    duepi_test_modules: SimpleNamespace, raw_value: object, expected: bool | None
) -> None:
    """Only documented online encodings reach the coordinator."""
    api = duepi_test_modules.api
    state = client(api, FakeSession([]))._parse_dashboard(dashboard(online=raw_value))

    assert state.raw_online is expected
    assert state.online is expected


def test_read_server_error_retries_once_after_two_seconds(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A transient dashboard 5xx gets precisely one delayed retry."""
    api = duepi_test_modules.api
    session = FakeSession([FakeResponse(500), FakeResponse(200, dashboard())])
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    state = asyncio.run(cloud_client.async_get_stove_state())

    assert state.power_on is True
    assert len(session.get_calls) == 2
    assert sleeps == [2]


def test_read_transport_error_retries_once_after_two_seconds(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A transport failure gets precisely one delayed dashboard retry."""
    api = duepi_test_modules.api
    session = FakeSession(
        [api.aiohttp.ClientError("network"), FakeResponse(200, dashboard())]
    )
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    state = asyncio.run(cloud_client.async_get_stove_state())

    assert state.power_on is True
    assert len(session.get_calls) == 2
    assert sleeps == [2]


def test_read_rate_limit_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A rate limit is surfaced, rather than retried as a transport problem."""
    api = duepi_test_modules.api
    session = FakeSession([FakeResponse(429)])
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    with pytest.raises(api.DuepiRateLimitError):
        asyncio.run(cloud_client.async_get_stove_state())

    assert len(session.get_calls) == 1
    assert sleeps == []


def test_command_server_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A potentially non-idempotent command is sent once, even when the server fails."""
    api = duepi_test_modules.api
    session = FakeSession([], [FakeResponse(500)])
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    with pytest.raises(api.DuepiServerError):
        asyncio.run(cloud_client.async_turn_off())

    assert len(session.post_calls) == 1
    assert sleeps == []


def test_command_session_expiry_is_not_reauthenticated_or_retried(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """A command that loses its session propagates the failure after one request."""
    api = duepi_test_modules.api
    session = FakeSession([], [FakeResponse(302, headers={"Location": "/login"})])
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    login_calls = 0

    async def unexpected_login() -> bool:
        nonlocal login_calls
        login_calls += 1
        return True

    monkeypatch.setattr(cloud_client, "async_login", unexpected_login)
    with pytest.raises(api.DuepiSessionExpiredError):
        asyncio.run(cloud_client.async_turn_off())

    assert len(session.post_calls) == 1
    assert login_calls == 0


def test_session_expiry_has_a_distinct_auth_error_type(
    duepi_test_modules: SimpleNamespace,
) -> None:
    """Expired sessions remain distinguishable from invalid supplied credentials."""
    api = duepi_test_modules.api
    cloud_client = client(api, FakeSession([FakeResponse(302, headers={"Location": "/login"})]))
    cloud_client._authenticated = True

    with pytest.raises(api.DuepiSessionExpiredError):
        asyncio.run(cloud_client._fetch_dashboard())


@pytest.mark.parametrize(
    ("error_class", "expected_message"),
    [
        ("DuepiTransportError", "Cannot connect to dpremoteiot.com: connection lost"),
        ("DuepiServerError", "Cloud service server error: service unavailable"),
        ("DuepiRateLimitError", "Cloud service rate limit: too many requests"),
    ],
)
def test_coordinator_reports_transport_server_and_rate_limit_separately(
    duepi_test_modules: SimpleNamespace,
    error_class: str,
    expected_message: str,
) -> None:
    """Coordinator failures preserve the cloud error category for the user."""
    api = duepi_test_modules.api

    class FailingClient:
        async def async_get_stove_state(self) -> object:
            raise getattr(api, error_class)(
                {
                    "DuepiTransportError": "connection lost",
                    "DuepiServerError": "service unavailable",
                    "DuepiRateLimitError": "too many requests",
                }[error_class]
            )

    coordinator = duepi_test_modules.coordinator.DuepiCoordinator(
        object(),
        SimpleNamespace(async_create_background_task=lambda *_args, **_kwargs: None),
        FailingClient(),
        update_interval=None,
    )
    with pytest.raises(duepi_test_modules.coordinator.UpdateFailed, match=expected_message):
        asyncio.run(coordinator._async_update_data())


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        ("invalid_credentials", "invalid_auth"),
        ("parse", "invalid_device"),
        ("transport", "cannot_connect"),
        ("server", "server_error"),
        ("rate_limit", "rate_limited"),
    ],
)
def test_config_validation_maps_each_error_category_to_its_own_form_error(
    monkeypatch: pytest.MonkeyPatch,
    duepi_test_modules: SimpleNamespace,
    outcome: str,
    expected_error: str,
) -> None:
    """The setup form distinguishes credentials, device data, and cloud failures."""
    api = duepi_test_modules.api
    config_flow = duepi_test_modules.config_flow

    class Session:
        async def close(self) -> None:
            return None

    class StubClient:
        def __init__(self, *_args: object) -> None:
            pass

        async def async_login(self) -> bool:
            return outcome != "invalid_credentials"

        async def async_get_stove_state(self) -> object:
            errors = {
                "parse": api.DuepiParseError("unknown device"),
                "transport": api.DuepiTransportError("network down"),
                "server": api.DuepiServerError("maintenance"),
                "rate_limit": api.DuepiRateLimitError("slow down"),
            }
            if outcome in errors:
                raise errors[outcome]
            return object()

    monkeypatch.setattr(config_flow.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(config_flow, "DuepiCloudClient", StubClient)
    flow = config_flow.DuepiConfigFlow()

    assert (
        asyncio.run(flow._async_validate_credentials("user@example.invalid", "secret", DEVICE_ID))
        == expected_error
    )


def test_session_expiry_reauthenticates_without_a_transient_retry(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """Session expiry uses one reauthentication path, without a retry sleep."""
    api = duepi_test_modules.api
    session = FakeSession(
        [
            FakeResponse(302, headers={"Location": "/login"}),
            FakeResponse(200, "<form></form>"),
            FakeResponse(200, dashboard()),
        ],
        [FakeResponse(302, headers={"Location": "/dashboard"})],
    )
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    state = asyncio.run(cloud_client.async_get_stove_state())

    assert state.power_on is True
    assert [url for url, _kwargs in session.get_calls].count(api.URL_DASHBOARD) == 2
    assert sleeps == []


def test_parse_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, duepi_test_modules: SimpleNamespace
) -> None:
    """Incompatible dashboard HTML is surfaced immediately without a retry."""
    api = duepi_test_modules.api
    session = FakeSession([FakeResponse(200, "<html>maintenance</html>")])
    cloud_client = client(api, session)
    cloud_client._authenticated = True
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(api.asyncio, "sleep", record_sleep)
    with pytest.raises(api.DuepiParseError):
        asyncio.run(cloud_client.async_get_stove_state())

    assert len(session.get_calls) == 1
    assert sleeps == []
