"""API client for dpremoteiot.com cloud service."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass

import aiohttp

from .const import DEFAULT_POWER, DEFAULT_TEMPERATURE, URL_DASHBOARD, URL_LOGIN, URL_SET_SETTINGS

_LOGGER = logging.getLogger(__name__)

# --- HTTP constants ---
TIMEOUT_DEFAULT = aiohttp.ClientTimeout(total=15)
TIMEOUT_COMMAND = aiohttp.ClientTimeout(total=10)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
    "Referer": "https://dpremoteiot.com/dashboard",
    "Origin": "https://dpremoteiot.com",
}

HEADERS_FORM = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}

# --- Pre-compiled regex patterns ---
_RE_POWER_STATUS = re.compile(r"Power Status\s*:?\s*(ON|OFF)", re.IGNORECASE)
_RE_POWER_STATE = re.compile(r"powerState.*?(ON|OFF)", re.DOTALL | re.IGNORECASE)
_RE_STATUS_TEXT = re.compile(
    r"Status\s*:?\s*\n?\s*((?:Heating|Cooling|Standby|Off|Idle)[\w\s/°.]*\d*)",
    re.IGNORECASE,
)
_RE_ROOM_TEMP = re.compile(r"Room Temperature\s*(\d+)", re.IGNORECASE)
_RE_SETTED_POWER = re.compile(r'settedPower.*?value="(\d+)"', re.DOTALL | re.IGNORECASE)
_RE_WORKING_POWER = re.compile(r'Working Power.*?<input[^>]*value="(\d+)"', re.DOTALL | re.IGNORECASE)
_RE_SETTED_TEMP = re.compile(r'settedTemperature.*?value="(\d+)"', re.DOTALL | re.IGNORECASE)
_RE_SET_TEMP = re.compile(r'Set Temperature.*?<input[^>]*value="(\d+)"', re.DOTALL | re.IGNORECASE)
_RE_ONLINE = re.compile(r"Status\s*:?\s*<[^>]*>(Online|Offline)", re.IGNORECASE)
_RE_DEVICE_ID = re.compile(r'deviceid[=\s"\']+([a-f0-9]{24})', re.IGNORECASE)
_RE_CSRF_INPUT = re.compile(r'<input[^>]*name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']', re.IGNORECASE)
_RE_CSRF_INPUT_ALT = re.compile(r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']_csrf["\']', re.IGNORECASE)
_RE_CSRF_META = re.compile(r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']', re.IGNORECASE)


def _safe_int(value: object) -> int | None:
    """Convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value: object) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _normalize_online(value: object) -> bool | None:
    """Normalize the limited set of online values emitted by the cloud API."""
    if isinstance(value, bool):
        return value
    if type(value) is int:
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "online":
            return True
        if normalized == "offline":
            return False
    return None


class DuepiApiError(Exception):
    """Base exception for Duepi API errors."""


class DuepiAuthError(DuepiApiError):
    """Authentication failure."""


class DuepiInvalidCredentialsError(DuepiAuthError):
    """The supplied account credentials were rejected."""


class DuepiSessionExpiredError(DuepiAuthError):
    """A previously authenticated cloud session is no longer valid."""


class DuepiConnectionError(DuepiApiError):
    """Network connectivity error."""


class DuepiTransportError(DuepiConnectionError):
    """A network or timeout failure eligible for one read retry."""


class DuepiServerError(DuepiConnectionError):
    """A transient server-side HTTP failure eligible for one read retry."""


class DuepiRateLimitError(DuepiConnectionError):
    """The cloud service refused a request due to rate limiting."""


class DuepiParseError(DuepiApiError):
    """HTML parsing failure."""


@dataclass(slots=True)
class DuepiStoveState:
    """Represents the current state of the stove."""

    power_on: bool
    status_text: str | None
    room_temperature: float | None
    working_power: int | None
    set_temperature: int | None
    raw_online: bool | None
    online: bool | None


class DuepiCloudClient:
    """Async client for the dpremoteiot.com cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        device_id: str,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._device_id = device_id
        self._authenticated = False
        self._auth_lock = asyncio.Lock()
        self._api_device_id: str | None = None  # MongoDB ObjectId, resolved from dashboard

    @property
    def device_id(self) -> str:
        """Return the device ID."""
        return self._device_id

    async def async_close(self) -> None:
        """Retain compatibility without closing Home Assistant's session."""

    async def async_login(self) -> bool:
        """Authenticate with dpremoteiot.com and obtain a session cookie.

        Returns True on success, False on invalid credentials.
        Raises DuepiConnectionError on network issues.
        """
        async with self._auth_lock:
            try:
                async with self._session.get(
                    URL_LOGIN,
                    headers=HEADERS,
                    allow_redirects=False,
                    timeout=TIMEOUT_DEFAULT,
                ) as resp:
                    if 300 <= resp.status < 400:
                        raise DuepiConnectionError("Unexpected redirect while loading login")
                    self._raise_for_http_error(resp)
                    login_html = await resp.text()

                csrf_token = self._extract_csrf(login_html)

                data: dict[str, str] = {
                    "email": self._email,
                    "password": self._password,
                }
                if csrf_token:
                    data["_csrf"] = csrf_token

                async with self._session.post(
                    URL_LOGIN,
                    data=data,
                    headers=HEADERS_FORM,
                    allow_redirects=False,
                    timeout=TIMEOUT_DEFAULT,
                ) as resp:
                    if 300 <= resp.status < 400:
                        location = resp.headers.get("Location", "")
                        if self._is_dashboard_redirect(location):
                            self._authenticated = True
                            _LOGGER.debug("Login successful")
                            return True
                        raise DuepiConnectionError("Unexpected redirect during login")

                    if resp.status == 429:
                        raise DuepiRateLimitError("Login request was rate limited")
                    if resp.status >= 500:
                        raise DuepiServerError("Login service returned a server error")
                    if resp.status in (401, 403):
                        self._authenticated = False
                        return False
                    self._raise_for_http_error(resp)

                    if resp.status == 200:
                        body = await resp.text()
                        if "dashboard" in body.lower() and "sign in" not in body.lower():
                            self._authenticated = True
                            _LOGGER.debug("Login successful (200 with dashboard content)")
                            return True

                self._authenticated = False
                _LOGGER.warning("Login failed")
                return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                self._authenticated = False
                raise DuepiTransportError("Cannot connect to dpremoteiot.com") from err

    async def async_get_stove_state(self) -> DuepiStoveState:
        """Fetch the dashboard and parse the stove state.

        Raises DuepiAuthError, DuepiConnectionError, or DuepiParseError.
        """
        await self._ensure_auth()

        try:
            page_html = await self._fetch_dashboard()
        except DuepiSessionExpiredError:
            self._authenticated = False
            await self._ensure_auth()
            page_html = await self._fetch_dashboard()
        except (DuepiTransportError, DuepiServerError):
            await asyncio.sleep(2)
            try:
                page_html = await self._fetch_dashboard()
            except DuepiSessionExpiredError:
                self._authenticated = False
                await self._ensure_auth()
                page_html = await self._fetch_dashboard()

        return self._parse_dashboard(page_html)

    async def async_turn_on(self, power: int | None = None, temperature: int | None = None) -> None:
        """Turn the stove on."""
        await self._send_command(active=True, power=power, temperature=temperature)

    async def async_turn_off(self) -> None:
        """Turn the stove off."""
        await self._send_command(active=False)

    async def async_set_power(self, power: int, current_state: DuepiStoveState | None = None) -> None:
        """Set the working power level (1-5) without changing on/off state."""
        if current_state is None:
            current_state = await self.async_get_stove_state()
        await self._send_command(
            active=current_state.power_on,
            power=power,
            temperature=current_state.set_temperature,
        )

    async def async_set_temperature(self, temperature: int, current_state: DuepiStoveState | None = None) -> None:
        """Set the target temperature (5-40°C) without changing on/off state."""
        if current_state is None:
            current_state = await self.async_get_stove_state()
        await self._send_command(
            active=current_state.power_on,
            power=current_state.working_power,
            temperature=temperature,
        )

    # --- Private methods ---

    async def _ensure_auth(self) -> None:
        """Ensure we have a valid session, logging in if needed."""
        if not self._authenticated:
            if not await self.async_login():
                raise DuepiInvalidCredentialsError("Login failed with provided credentials")

    async def _fetch_dashboard(self) -> str:
        """Fetch the dashboard HTML, detecting session expiry."""
        try:
            async with self._session.get(
                URL_DASHBOARD,
                headers=HEADERS,
                allow_redirects=False,
                timeout=TIMEOUT_DEFAULT,
            ) as resp:
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location", "")
                    if self._is_login_redirect(location):
                        self._authenticated = False
                        raise DuepiSessionExpiredError("Session expired (redirected to login)")
                    raise DuepiConnectionError("Unexpected redirect while reading dashboard")

                self._raise_for_http_error(resp, authentication_failure=True)
                page_html = await resp.text()

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DuepiTransportError("Cannot reach dpremoteiot.com") from err

        html_lower = page_html[:2000].lower()
        if "sign in" in html_lower or ("login" in html_lower and "<form" in html_lower):
            self._authenticated = False
            raise DuepiSessionExpiredError("Session expired (received login page)")

        return page_html

    @staticmethod
    def _is_dashboard_redirect(location: str) -> bool:
        """Return whether a login redirect leads to the expected dashboard."""
        return location in {"/", "/dashboard"} or location.startswith(("/?", "/dashboard?"))

    @staticmethod
    def _is_login_redirect(location: str) -> bool:
        """Return whether a response redirect unambiguously targets login."""
        return location == "/login" or location.startswith("/login?")

    def _raise_for_http_error(
        self, response: aiohttp.ClientResponse, *, authentication_failure: bool = False
    ) -> None:
        """Convert an HTTP error status to the integration's safe error taxonomy."""
        if response.status == 429:
            raise DuepiRateLimitError("Cloud service rate limited the request")
        if response.status >= 500:
            raise DuepiServerError("Cloud service returned a server error")
        if 400 <= response.status < 500:
            if authentication_failure and response.status in (401, 403):
                self._authenticated = False
                raise DuepiSessionExpiredError("Session was rejected by the cloud service")
            raise DuepiConnectionError("Cloud service rejected the request")

    async def _send_command(
        self,
        active: bool,
        power: int | None = None,
        temperature: int | None = None,
    ) -> None:
        """Send a control command to the stove."""
        await self._ensure_auth()

        effective_id = self._api_device_id or self._device_id
        data = {
            "deviceId": effective_id,
            "active": "1" if active else "0",
            "emailNotifications": "0",
            "settedPower": str(power if power is not None else DEFAULT_POWER),
            "settedTemperature": str(temperature if temperature is not None else DEFAULT_TEMPERATURE),
            "switch": "on" if active else "off",
        }

        try:
            async with self._session.post(
                URL_SET_SETTINGS,
                data=data,
                headers=HEADERS_FORM,
                allow_redirects=False,
                timeout=TIMEOUT_COMMAND,
            ) as resp:
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location", "")
                    if self._is_login_redirect(location):
                        self._authenticated = False
                        raise DuepiSessionExpiredError("Session expired during command")
                    raise DuepiConnectionError("Unexpected redirect during command")
                self._raise_for_http_error(resp, authentication_failure=True)
                _LOGGER.debug("Command sent successfully")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DuepiTransportError("Failed to send command") from err

    def _parse_dashboard(self, page_html: str) -> DuepiStoveState:
        """Parse the dashboard HTML to extract stove state.

        The dpremoteiot.com dashboard embeds device data as JSON inside HTML
        comments with HTML-encoded quotes (&#34;). A dashboard is accepted only
        when it contains the configured device and its current settings.
        """
        device_json = self._extract_device_json(page_html)
        if device_json is None:
            raise DuepiParseError("Configured device was not found in the dashboard")

        settings = device_json.get("deviceCurrentSettings")
        if not isinstance(settings, dict):
            raise DuepiParseError("Configured device has no current settings")

        power_state = settings.get("powerState")
        if not isinstance(power_state, str) or power_state.upper() not in {"ON", "OFF"}:
            raise DuepiParseError("Configured device has invalid current settings")

        api_id = device_json.get("_id")
        if isinstance(api_id, str) and api_id:
            self._api_device_id = api_id

        online = _normalize_online(settings.get("isOnline"))

        return DuepiStoveState(
            power_on=power_state.upper() == "ON",
            status_text=settings.get("status") if isinstance(settings.get("status"), str) else None,
            room_temperature=_safe_float(settings.get("roomTemperature")),
            working_power=_safe_int(settings.get("settedPower")),
            set_temperature=_safe_int(settings.get("settedTemperature")),
            raw_online=online,
            online=online,
        )

    def _extract_device_json(self, page_html: str) -> dict | None:
        """Extract device data from JSON embedded in HTML comments.

        The dashboard contains comments like:
        <!-- [{"_id":"...","univocalID":"...","deviceCurrentSettings":{...}}] -->
        with quotes encoded as &#34;
        """
        # Find all HTML comments that contain our device ID
        for comment_match in re.finditer(r"<!--(.*?)-->", page_html, re.DOTALL):
            comment = comment_match.group(1).strip()
            if self._device_id not in comment:
                continue

            # Decode HTML entities (&#34; -> ")
            decoded = html.unescape(comment)

            try:
                data = json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                _LOGGER.debug("Found device ID in comment but JSON parse failed")
                continue

            candidates = data if isinstance(data, list) else [data]
            for device in candidates:
                if not isinstance(device, dict):
                    continue
                if (
                    device.get("_id") == self._device_id
                    or device.get("univocalID") == self._device_id
                ):
                    return device

        _LOGGER.debug("No matching device data found in dashboard")
        return None

    @staticmethod
    def _extract_csrf(login_html: str) -> str | None:
        """Extract a CSRF token from the login page HTML."""
        for pattern in (_RE_CSRF_INPUT, _RE_CSRF_INPUT_ALT, _RE_CSRF_META):
            match = pattern.search(login_html)
            if match:
                return match.group(1)
        return None
