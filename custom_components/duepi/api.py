"""API client for dpremoteiot.com cloud service."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

from .const import URL_DASHBOARD, URL_LOGIN, URL_SET_SETTINGS

_LOGGER = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
    "Referer": "https://dpremoteiot.com/dashboard",
    "Origin": "https://dpremoteiot.com",
}


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

    @property
    def device_id(self) -> str:
        """Return the device ID."""
        return self._device_id

    async def async_login(self) -> bool:
        """Authenticate with dpremoteiot.com and obtain a session cookie.

        Returns True on success, False on invalid credentials.
        Raises DuepiConnectionError on network issues.
        """
        async with self._auth_lock:
            try:
                # Step 1: GET /login to find CSRF token
                async with self._session.get(
                    URL_LOGIN, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    login_html = await resp.text()

                csrf_token = self._extract_csrf(login_html)

                # Step 2: POST /login with credentials
                data: dict[str, str] = {
                    "email": self._email,
                    "password": self._password,
                }
                if csrf_token:
                    data["_csrf"] = csrf_token

                async with self._session.post(
                    URL_LOGIN,
                    data=data,
                    headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (301, 302):
                        location = resp.headers.get("Location", "")
                        if "/dashboard" in location or location == "/":
                            self._authenticated = True
                            _LOGGER.debug("Login successful (redirect to %s)", location)
                            return True

                    # Check if we landed on the dashboard directly (some servers don't redirect)
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
            # Session expired, re-login once
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

    async def async_set_power(self, power: int) -> None:
        """Set the working power level (1-5) without changing on/off state."""
        state = await self.async_get_stove_state()
        await self._send_command(
            active=state.power_on,
            power=power,
            temperature=state.set_temperature,
        )

    async def async_set_temperature(self, temperature: int) -> None:
        """Set the target temperature (0-35) without changing on/off state."""
        state = await self.async_get_stove_state()
        await self._send_command(
            active=state.power_on,
            power=state.working_power,
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
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (301, 302):
                    location = resp.headers.get("Location", "")
                    if "/login" in location:
                        self._authenticated = False
                        raise DuepiAuthError("Session expired (redirected to login)")
                    # Follow other redirects
                    async with self._session.get(
                        location,
                        headers=HEADERS,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp2:
                        html = await resp2.text()
                else:
                    html = await resp.text()

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DuepiConnectionError(f"Cannot reach dpremoteiot.com: {err}") from err

        # Check if we got the login page instead of the dashboard
        if "sign in" in html[:1000].lower() or "<form" in html[:1000].lower() and "login" in html[:2000].lower():
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

        from .const import DEFAULT_POWER, DEFAULT_TEMPERATURE

        data = {
            "deviceId": self._device_id,
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
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (301, 302):
                    location = resp.headers.get("Location", "")
                    if "/login" in location:
                        self._authenticated = False
                        raise DuepiAuthError("Session expired during command")
                resp.raise_for_status()
                _LOGGER.debug(
                    "Command sent: active=%s power=%s temp=%s (HTTP %d)",
                    active, power, temperature, resp.status,
                )
        except DuepiAuthError:
            # Retry once after re-login
            self._authenticated = False
            await self._ensure_auth()
            async with self._session.post(
                URL_SET_SETTINGS,
                data=data,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DuepiConnectionError(f"Failed to send command: {err}") from err

    def _parse_dashboard(self, html: str) -> DuepiStoveState:
        """Parse the dashboard HTML to extract stove state using regex."""
        block = self._extract_device_block(html)

        # Power Status (ON/OFF)
        power_match = re.search(r"Power Status\s*:?\s*(ON|OFF)", block, re.IGNORECASE)
        if not power_match:
            power_match = re.search(r"powerState.*?(ON|OFF)", block, re.DOTALL | re.IGNORECASE)
        power_on = power_match.group(1).upper() == "ON" if power_match else False

        # Status text
        status_match = re.search(
            r"Status\s*:?\s*\n?\s*((?:Heating|Cooling|Standby|Off|Idle)[\w\s/°.]*\d*)",
            block,
            re.IGNORECASE,
        )
        status_text = status_match.group(1).strip() if status_match else None

        # Room temperature
        temp_match = re.search(r"Room Temperature\s*(\d+)", block, re.IGNORECASE)
        room_temp = float(temp_match.group(1)) if temp_match else None

        # Working Power from input field
        power_val_match = re.search(
            r'settedPower.*?value="(\d+)"', block, re.DOTALL | re.IGNORECASE
        )
        if not power_val_match:
            power_val_match = re.search(
                r'Working Power.*?<input[^>]*value="(\d+)"', block, re.DOTALL | re.IGNORECASE
            )
        working_power = int(power_val_match.group(1)) if power_val_match else None

        # Set Temperature from input field
        temp_val_match = re.search(
            r'settedTemperature.*?value="(\d+)"', block, re.DOTALL | re.IGNORECASE
        )
        if not temp_val_match:
            temp_val_match = re.search(
                r'Set Temperature.*?<input[^>]*value="(\d+)"', block, re.DOTALL | re.IGNORECASE
            )
        set_temp = int(temp_val_match.group(1)) if temp_val_match else None

        # Online/Offline badge
        online_match = re.search(
            r"Status\s*:?\s*<[^>]*>(Online|Offline)", block, re.IGNORECASE
        )
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
        pattern = rf"{re.escape(self._device_id)}.*?(?=deviceid=|$)"
        match = re.search(pattern, html, re.DOTALL)
        return match.group(0) if match else html

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        """Extract a CSRF token from the login page HTML."""
        # Common patterns: <input type="hidden" name="_csrf" value="...">
        # or <meta name="csrf-token" content="...">
        match = re.search(
            r'<input[^>]*name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)

        match = re.search(
            r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']_csrf["\']',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)

        match = re.search(
            r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)

        return None
