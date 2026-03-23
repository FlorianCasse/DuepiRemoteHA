"""API client for dpremoteiot.com cloud service."""

from __future__ import annotations

import asyncio
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


class DuepiApiError(Exception):
    """Base exception for Duepi API errors."""


class DuepiAuthError(DuepiApiError):
    """Authentication failure."""


class DuepiConnectionError(DuepiApiError):
    """Network connectivity error."""


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
        self._device_block_re = re.compile(
            rf"{re.escape(device_id)}.*?(?=deviceid=|$)", re.DOTALL
        )

    @property
    def device_id(self) -> str:
        """Return the device ID."""
        return self._device_id

    async def async_close(self) -> None:
        """Close the underlying HTTP session."""
        await self._session.close()

    async def async_login(self) -> bool:
        """Authenticate with dpremoteiot.com and obtain a session cookie.

        Returns True on success, False on invalid credentials.
        Raises DuepiConnectionError on network issues.
        """
        async with self._auth_lock:
            try:
                async with self._session.get(
                    URL_LOGIN, headers=HEADERS, timeout=TIMEOUT_DEFAULT
                ) as resp:
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
                    if resp.status in (301, 302):
                        location = resp.headers.get("Location", "")
                        if "/dashboard" in location or location == "/":
                            self._authenticated = True
                            _LOGGER.debug("Login successful (redirect to %s)", location)
                            return True

                    if resp.status == 200:
                        body = await resp.text()
                        if "dashboard" in body.lower() and "sign in" not in body.lower():
                            self._authenticated = True
                            _LOGGER.debug("Login successful (200 with dashboard content)")
                            return True

                self._authenticated = False
                _LOGGER.warning("Login failed: invalid credentials or unexpected response")
                return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                self._authenticated = False
                raise DuepiConnectionError(f"Cannot connect to dpremoteiot.com: {err}") from err

    async def async_get_stove_state(self) -> DuepiStoveState:
        """Fetch the dashboard and parse the stove state.

        Raises DuepiAuthError, DuepiConnectionError, or DuepiParseError.
        """
        await self._ensure_auth()

        try:
            html = await self._fetch_dashboard()
        except DuepiAuthError:
            self._authenticated = False
            await self._ensure_auth()
            html = await self._fetch_dashboard()

        return self._parse_dashboard(html)

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
        """Set the target temperature (0-35) without changing on/off state."""
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
                raise DuepiAuthError("Login failed with provided credentials")

    async def _fetch_dashboard(self) -> str:
        """Fetch the dashboard HTML, detecting session expiry."""
        try:
            async with self._session.get(
                URL_DASHBOARD,
                headers=HEADERS,
                allow_redirects=False,
                timeout=TIMEOUT_DEFAULT,
            ) as resp:
                if resp.status in (301, 302):
                    location = resp.headers.get("Location", "")
                    if "/login" in location:
                        self._authenticated = False
                        raise DuepiAuthError("Session expired (redirected to login)")
                    async with self._session.get(
                        location,
                        headers=HEADERS,
                        timeout=TIMEOUT_DEFAULT,
                    ) as resp2:
                        html = await resp2.text()
                else:
                    html = await resp.text()

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DuepiConnectionError(f"Cannot reach dpremoteiot.com: {err}") from err

        html_lower = html[:2000].lower()
        if "sign in" in html_lower or ("login" in html_lower and "<form" in html_lower):
            self._authenticated = False
            raise DuepiAuthError("Session expired (received login page)")

        return html

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
            "deviceid": effective_id,
            "active": "1" if active else "0",
            "emailNotifications": "0",
            "settedPower": str(power if power is not None else DEFAULT_POWER),
            "settedTemperature": str(temperature if temperature is not None else DEFAULT_TEMPERATURE),
        }

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with self._session.post(
                    URL_SET_SETTINGS,
                    data=data,
                    headers=HEADERS_FORM,
                    timeout=TIMEOUT_COMMAND,
                ) as resp:
                    if resp.status in (301, 302):
                        location = resp.headers.get("Location", "")
                        if "/login" in location:
                            self._authenticated = False
                            raise DuepiAuthError("Session expired during command")
                    if resp.status >= 500:
                        _LOGGER.warning(
                            "Server error on attempt %d: %d, message='%s', url='%s'",
                            attempt + 1, resp.status, resp.reason, resp.url,
                        )
                        last_err = DuepiConnectionError(
                            f"Failed to send command: {resp.status}, "
                            f"message='{resp.reason}', url='{resp.url}'"
                        )
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise last_err
                    resp.raise_for_status()
                    _LOGGER.debug(
                        "Command sent: active=%s power=%s temp=%s (HTTP %d)",
                        active, power, temperature, resp.status,
                    )
                return
            except DuepiAuthError:
                if attempt == 0:
                    self._authenticated = False
                    await self._ensure_auth()
                else:
                    raise
            except DuepiConnectionError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise DuepiConnectionError(f"Failed to send command: {err}") from err

    def _parse_dashboard(self, html: str) -> DuepiStoveState:
        """Parse the dashboard HTML to extract stove state."""
        # Extract the MongoDB ObjectId that precedes our short device ID
        # HTML structure: ...deviceid=<objectid>...<short_id>...deviceid=<next>...
        if not self._api_device_id:
            api_id_match = re.search(
                rf'deviceid=([a-f0-9]{{24}})(?:(?!deviceid=).)*?{re.escape(self._device_id)}',
                html, re.DOTALL | re.IGNORECASE,
            )
            if api_id_match:
                self._api_device_id = api_id_match.group(1)
                _LOGGER.debug("Resolved API device ID: %s", self._api_device_id)
            else:
                _LOGGER.warning("Could not resolve API device ID from dashboard HTML")

        block = self._extract_device_block(html)

        power_match = _RE_POWER_STATUS.search(block) or _RE_POWER_STATE.search(block)
        power_on = power_match.group(1).upper() == "ON" if power_match else False

        status_match = _RE_STATUS_TEXT.search(block)
        status_text = status_match.group(1).strip() if status_match else None

        temp_match = _RE_ROOM_TEMP.search(block)
        room_temp = float(temp_match.group(1)) if temp_match else None

        power_val_match = _RE_SETTED_POWER.search(block) or _RE_WORKING_POWER.search(block)
        working_power = int(power_val_match.group(1)) if power_val_match else None

        temp_val_match = _RE_SETTED_TEMP.search(block) or _RE_SET_TEMP.search(block)
        set_temp = int(temp_val_match.group(1)) if temp_val_match else None

        online_match = _RE_ONLINE.search(block)
        online = online_match.group(1).lower() == "online" if online_match else None

        return DuepiStoveState(
            power_on=power_on,
            status_text=status_text,
            room_temperature=room_temp,
            working_power=working_power,
            set_temperature=set_temp,
            online=online,
        )

    def _extract_device_block(self, html: str) -> str:
        """Extract the HTML block for our device from the dashboard."""
        match = self._device_block_re.search(html)
        return match.group(0) if match else html

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        """Extract a CSRF token from the login page HTML."""
        for pattern in (_RE_CSRF_INPUT, _RE_CSRF_INPUT_ALT, _RE_CSRF_META):
            match = pattern.search(html)
            if match:
                return match.group(1)
        return None
