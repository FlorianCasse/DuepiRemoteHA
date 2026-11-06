"""Client for the Duepi relay server used by the MyDPRemote app.

The stove's Wi-Fi module keeps a TCP connection to a Duepi relay
(``duepiwebserverN.com:<port>``). A client that logs in as ``master`` with the
module's device code can exchange AES-encrypted frames with the module:

    request = ESC + base64(IV16 || AES-128-CBC(key, IV, command + checksum)) + "&"
    key     = MD5(device code) XOR _KEY_MASK

Each public method opens its own connection, logs in, exchanges frames while
holding the client lock and closes the connection, inside a global time cap.
The device code is a secret: it never appears in logs or exception messages.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .api import (
    DuepiConnectionError,
    DuepiApiError,
    DuepiProtocolError,
    DuepiStartRefusedError,
    DuepiStoveOffError,
    DuepiStoveState,
    DuepiTransportError,
)
from .const import (
    DEFAULT_TEMPERATURE,
    MAX_POWER,
    MAX_TEMPERATURE,
    MIN_POWER,
    MIN_TEMPERATURE,
)

_LOGGER = logging.getLogger(__name__)

_KEY_MASK = bytes.fromhex("23FADC18A422678312C86AC29281AC01")
_FRAME_START = b"\x1b"
_FRAME_END = b"&"
_BLOCK = 16

CONNECT_TIMEOUT = 5.0
FRAME_TIMEOUT = 4.0
OPERATION_TIMEOUT = 15.0
LOGIN_DELAY = 0.5
# Bytes already waiting after the login are discarded for this long, so a late
# reply from an earlier session cannot be read as the answer to a request.
DRAIN_TIMEOUT = 0.05
# A second ignition is refused this long after the last attempt, even when the
# first one's reply was lost (the stove may already be starting).
IGNITION_COOLDOWN = 60.0
# After a turn-off the power stays locked until the stove leaves heating, or
# this long at most (a stove that ignored the command is usable again).
OFF_LATCH_TIMEOUT = 600.0
_MAX_FRAME_BYTES = 512

_READ_COMMAND = re.compile(r"^R[CD][0-9A-F]{4}$")
_WRITE_COMMAND = re.compile(r"^RF00[0-5]0$|^RF2[0-9A-F]{2}0$")
# Unencrypted frame the relay may interleave: ESC + 8 hex chars + "&".
_CLEARTEXT_FRAME = re.compile(rb"\x1b[0-9A-Fa-f]{8}&")

# Registers read on every poll (meanings observed on a real stove, 2026-11-06).
# RC5000/RC6000 only echo the last remote command, so they are not read.
REG_PHASE = "RD9000"
REG_POWER_SETTING = "RD3000"  # Power setting (panel P1-P5).
REG_REAL_POWER = "RD4000"  # Power the stove runs at; modulates below the setting.
REG_ROOM_TEMPERATURE = "RD1000"
REG_FUMES_TEMPERATURE = "RD0000"
REG_ERROR = "RDA000"
POLLED_REGISTERS = (
    REG_PHASE,
    REG_POWER_SETTING,
    REG_REAL_POWER,
    REG_ROOM_TEMPERATURE,
    REG_FUMES_TEMPERATURE,
    REG_ERROR,
)

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class PhaseInfo:
    """Meaning of a value of the phase register (RD9000)."""

    label: str
    # True only for stable heating phases where a power change is safe.
    running: bool
    flame_on: bool


PHASE_OFF = 0
PHASE_HEATING = 0x0201
STAGE_IGNITION = 0x01  # High byte of every ignition phase code (0x01xx).

# Codes observed on a real stove (2026-11-06). The high byte is the stage.
# Only stable heating is "running": power and setpoint writes are refused in
# every other phase, including unknown ones. Labels keep the keywords that
# climate.py and state_has_problem() parse ("heating", "idle", ...).
PHASES: dict[int, PhaseInfo] = {
    PHASE_OFF: PhaseInfo(label="Off", running=False, flame_on=False),
    0x0101: PhaseInfo(label="Ignition", running=False, flame_on=True),
    0x0102: PhaseInfo(label="Ignition", running=False, flame_on=True),
    0x0104: PhaseInfo(label="Ignition", running=False, flame_on=True),
    0x0200: PhaseInfo(label="Cleaning", running=False, flame_on=True),
    PHASE_HEATING: PhaseInfo(label="Heating", running=True, flame_on=True),
    0x0401: PhaseInfo(label="Cleaning", running=False, flame_on=True),
}


def derive_key(device_code: str) -> bytes:
    """Return the AES key for a device code."""
    digest = hashlib.md5(device_code.encode(), usedforsecurity=False).digest()
    return bytes(a ^ b for a, b in zip(digest, _KEY_MASK, strict=True))


def checksum(text: str) -> str:
    """Return the protocol checksum: byte sum mod 256 as 2 uppercase hex digits."""
    return f"{sum(text.encode('latin-1')) & 0xFF:02X}"


def relay_device_id(device_code: str) -> str:
    """Return a stable identifier that does not reveal the device code."""
    return "relay_" + hashlib.sha256(device_code.encode()).hexdigest()[:8]


def encode_frame(key: bytes, command: str, iv: bytes | None = None) -> bytes:
    """Encrypt a 6-character command into a relay frame."""
    if len(command) != 6:
        raise ValueError("Relay commands are exactly 6 characters")
    plaintext = (command + checksum(command)).encode("latin-1")
    plaintext = plaintext.ljust(_BLOCK, b"\0")
    iv = os.urandom(_BLOCK) if iv is None else iv
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return _FRAME_START + base64.b64encode(iv + ciphertext) + _FRAME_END


def decode_frame(key: bytes, frame: bytes) -> str:
    """Decrypt a relay frame and return its plaintext without padding."""
    body = frame.strip().removeprefix(_FRAME_START).removesuffix(_FRAME_END)
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as err:
        raise DuepiProtocolError("Relay reply is not valid base64") from err
    if len(raw) < 2 * _BLOCK or len(raw) % _BLOCK:
        raise DuepiProtocolError("Relay reply has an invalid length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:_BLOCK])).decryptor()
    plaintext = decryptor.update(raw[_BLOCK:]) + decryptor.finalize()
    return plaintext.split(b"\0", 1)[0].decode("latin-1")


def parse_reply(plaintext: str) -> int:
    """Validate a reply's checksum and return its value."""
    if len(plaintext) != 8:
        raise DuepiProtocolError("Relay reply has an unexpected length")
    data, received = plaintext[:6], plaintext[6:]
    if checksum(data) != received.upper():
        raise DuepiProtocolError("Relay reply failed its checksum")
    try:
        return int(data[:4], 16)
    except ValueError as err:
        raise DuepiProtocolError("Relay reply value is not hexadecimal") from err


def validate_write(command: str) -> str:
    """Return the command if it is an allowed write, otherwise raise ValueError."""
    if not _WRITE_COMMAND.fullmatch(command):
        raise ValueError(f"Relay write command not allowed: {command!r}")
    return command


def power_command(power: int) -> str:
    """Return the write that selects a power level (it ignites a stopped stove)."""
    if not isinstance(power, int) or not MIN_POWER <= power <= MAX_POWER:
        raise ValueError(f"Power must be {MIN_POWER}-{MAX_POWER}, got {power!r}")
    return validate_write(f"RF00{power}0")


def setpoint_command(temperature: int) -> str:
    """Return the write that sets the target temperature."""
    if not isinstance(temperature, int) or not (
        MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE
    ):
        raise ValueError(
            f"Temperature must be {MIN_TEMPERATURE}-{MAX_TEMPERATURE}, got {temperature!r}"
        )
    return validate_write(f"RF2{temperature:02X}0")


def is_running(phase: int, real_power: int) -> bool:
    """Return True only when a power or setpoint write is safe (stable heating)."""
    info = PHASES.get(phase)
    return bool(info and info.running) and real_power >= 1


def state_from_registers(
    registers: dict[str, int], set_temperature: int | None = None
) -> DuepiStoveState:
    """Map raw register values to the integration's stove state.

    The stove does not expose its setpoint, so ``set_temperature`` is the last
    value Home Assistant sent (or will send), supplied by the client.
    """
    phase = registers[REG_PHASE]
    info = PHASES.get(phase)
    power_setting = registers[REG_POWER_SETTING]
    real_power = registers[REG_REAL_POWER]
    error_code = registers[REG_ERROR]
    return DuepiStoveState(
        power_on=phase != PHASE_OFF,
        status_text=info.label if info else f"Phase {phase}",
        room_temperature=registers[REG_ROOM_TEMPERATURE] / 10,
        working_power=power_setting or None,
        set_temperature=set_temperature,
        raw_online=True,
        online=True,
        alarm=f"E{error_code}" if error_code else None,
        flame_on=info.flame_on if info else None,
        real_power=real_power,
        power_setting=power_setting,
        phase=phase,
        fumes_temperature=float(registers[REG_FUMES_TEMPERATURE]),
        error_code=error_code,
    )


async def _drain(reader: asyncio.StreamReader) -> None:
    """Discard bytes already waiting so they cannot answer the first request."""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(_MAX_FRAME_BYTES), DRAIN_TIMEOUT)
        except TimeoutError:
            return
        if not chunk:
            return
        _LOGGER.debug("Discarded %d bytes waiting after login", len(chunk))


async def _read(session: _Session, registers: tuple[str, ...]) -> dict[str, int]:
    """Read registers in order on an open session."""
    return {register: parse_reply(await session.exchange(register)) for register in registers}


async def _write(session: _Session, command: str) -> None:
    """Send one allowed write; its reply only proves delivery (checksum)."""
    parse_reply(await session.exchange(validate_write(command)))


class _Session:
    """One open, logged-in connection to the relay."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, key: bytes
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._key = key

    async def exchange(self, command: str) -> str:
        """Send one command and return the decrypted reply plaintext."""
        self._writer.write(encode_frame(self._key, command))
        await self._writer.drain()
        frame = await self._read_frame(command)
        return decode_frame(self._key, frame)

    async def _read_frame(self, command: str) -> bytes:
        """Return the next encrypted frame, skipping unsolicited cleartext ones.

        During ignition the relay was seen sending a cleartext frame
        (ESC + 8 hex chars + "&") before the encrypted reply. Cleartext is not
        authenticated by the device key, so it is never used as stove state.
        """
        buffer = b""
        while True:
            end = buffer.find(_FRAME_END)
            while end != -1:
                start = buffer.rfind(_FRAME_START, 0, end)
                if start == -1:
                    # Malformed relay output, not a decryption failure: never
                    # reported as a wrong device code.
                    raise DuepiTransportError(f"Relay reply to {command} has no frame start")
                frame, buffer = buffer[start : end + 1], buffer[end + 1 :]
                if _CLEARTEXT_FRAME.fullmatch(frame):
                    _LOGGER.debug("Skipping cleartext relay frame while waiting for %s", command)
                    end = buffer.find(_FRAME_END)
                    continue
                return frame
            if len(buffer) > _MAX_FRAME_BYTES:
                raise DuepiTransportError(f"Relay reply to {command} is too long")
            try:
                chunk = await asyncio.wait_for(self._reader.read(256), FRAME_TIMEOUT)
            except TimeoutError as err:
                raise DuepiTransportError(f"No relay reply to {command}") from err
            if not chunk:
                raise DuepiTransportError(f"Relay closed the connection during {command}")
            buffer += chunk


class DuepiRelayClient:
    """Async client for a stove reached through the Duepi relay.

    Safety rules (a power command sent to a stopped stove starts ignition):
    - only ``async_turn_on(..., ignite=True)`` may send a power command to a
      stopped stove, and only when the stove reads as off;
    - every other power or setpoint write first re-reads the phase and the real
      power in the same locked connection and needs stable heating;
    - the setpoint cannot be read back, so it is never sent before ignition:
      it is queued and sent once the stove reaches stable heating.
    """

    transport_name = "the Duepi relay"
    # RD4000 is the power the stove runs at, not the last requested level.
    reports_real_power = True

    def __init__(
        self,
        host: str,
        port: int,
        device_code: str,
        *,
        default_temperature: int = DEFAULT_TEMPERATURE,
    ) -> None:
        self._host = host
        self._port = port
        self._device_code = device_code
        self._key = derive_key(device_code)
        self._device_id = relay_device_id(device_code)
        self._lock = asyncio.Lock()
        # Last setpoint sent or queued; the stove does not expose it.
        self._setpoint = default_temperature
        self._setpoint_pending = False
        # Set by turn_off: power writes stay refused until a poll sees the stove
        # leave stable heating, so a command cannot cancel the shutdown.
        self._off_latched_at: float | None = None
        self._last_ignition: float | None = None
        # Event-loop time by which the running operation must finish.
        self._deadline = 0.0

    @property
    def device_id(self) -> str:
        """Return the identifier derived from the device code."""
        return self._device_id

    async def async_close(self) -> None:
        """Nothing to release: every operation closes its own connection."""

    async def async_get_stove_state(self) -> DuepiStoveState:
        """Read every polled register; send a queued setpoint once heating."""

        async def _poll(session: _Session) -> dict[str, int]:
            registers = await _read(session, POLLED_REGISTERS)
            if registers[REG_PHASE] != PHASE_HEATING:
                self._off_latched_at = None
            time_left = self._deadline - asyncio.get_running_loop().time()
            if (
                self._setpoint_pending
                and time_left > FRAME_TIMEOUT
                and self._writable(registers[REG_PHASE], registers[REG_REAL_POWER])
            ):
                try:
                    await _write(session, setpoint_command(self._setpoint))
                except (DuepiApiError, ConnectionError, OSError) as err:
                    # Keep the state just read; retry on the next poll.
                    _LOGGER.warning("Queued setpoint not delivered, will retry: %s", err)
                else:
                    self._setpoint_pending = False
                    _LOGGER.debug("Sent the queued setpoint %d", self._setpoint)
            return registers

        registers = await self._run(_poll)
        return state_from_registers(registers, self._setpoint)

    async def async_read_register(self, register: str) -> int:
        """Read one register (used to validate a new configuration)."""
        if not _READ_COMMAND.fullmatch(register):
            raise ValueError(f"Not a relay read command: {register!r}")

        async def _read_one(session: _Session) -> int:
            return parse_reply(await session.exchange(register))

        return await self._run(_read_one)

    async def async_turn_on(
        self,
        power: int | None = None,
        temperature: int | None = None,
        *,
        ignite: bool = False,
    ) -> None:
        """Start a stopped stove (``ignite=True`` only) or apply targets while heating."""
        if power is None:
            raise ValueError("A power level is required to turn the stove on")
        power_write = power_command(power)
        if temperature is not None:
            setpoint_command(temperature)

        async def _turn_on(session: _Session) -> None:
            phase, real_power, error = (
                await _read(session, (REG_PHASE, REG_REAL_POWER, REG_ERROR))
            ).values()
            if self._writable(phase, real_power):
                await _write(session, power_write)
                if temperature is not None:
                    await self._send_setpoint(session, temperature)
                return
            if not ignite:
                raise DuepiStoveOffError("The stove is not heating; refusing to start it")
            if phase == PHASE_OFF:
                if error or real_power:
                    raise DuepiStartRefusedError(
                        f"Stove reports error {error} / power {real_power}; not starting"
                    )
                now = time.monotonic()
                if (
                    self._last_ignition is not None
                    and now - self._last_ignition < IGNITION_COOLDOWN
                ):
                    raise DuepiStartRefusedError("A start was requested less than a minute ago")
                if temperature is not None:
                    self._setpoint, self._setpoint_pending = temperature, True
                # Recorded before sending: a lost reply may still mean "started".
                self._last_ignition = now
                # The power command ignites; the setpoint follows once heating.
                await _write(session, power_write)
                return
            if phase >> 8 == STAGE_IGNITION:
                # Already igniting: nothing to send, the setpoint follows.
                if temperature is not None:
                    self._setpoint, self._setpoint_pending = temperature, True
                return
            raise DuepiStartRefusedError(
                f"Stove is busy (phase {phase}); not starting it now"
            )

        await self._run(_turn_on)

    async def async_turn_off(self) -> None:
        """Turn the stove off and drop any queued setpoint."""

        async def _turn_off(session: _Session) -> None:
            # Locked before sending: a lost reply may still mean "shutting down".
            self._setpoint_pending = False
            self._off_latched_at = time.monotonic()
            await _write(session, validate_write("RF0000"))

        await self._run(_turn_off)

    async def async_set_power(
        self, power: int, current_state: DuepiStoveState | None = None
    ) -> None:
        """Change the power of a heating stove; refuse in any other phase."""
        power_write = power_command(power)

        async def _set_power(session: _Session) -> None:
            phase, real_power = (await _read(session, (REG_PHASE, REG_REAL_POWER))).values()
            if not self._writable(phase, real_power):
                raise DuepiStoveOffError("The stove is not heating; refusing to change power")
            await _write(session, power_write)

        await self._run(_set_power)

    async def async_set_temperature(
        self, temperature: int, current_state: DuepiStoveState | None = None
    ) -> None:
        """Send the setpoint while heating; otherwise queue it until heating."""
        setpoint_write = setpoint_command(temperature)

        async def _set_temperature(session: _Session) -> None:
            phase, real_power = (await _read(session, (REG_PHASE, REG_REAL_POWER))).values()
            if self._writable(phase, real_power):
                await self._send_setpoint(session, temperature, setpoint_write)
            else:
                self._setpoint, self._setpoint_pending = temperature, True

        await self._run(_set_temperature)

    def _writable(self, phase: int, real_power: int) -> bool:
        """Power and setpoint writes need stable heating and no pending turn-off."""
        if (
            self._off_latched_at is not None
            and time.monotonic() - self._off_latched_at > OFF_LATCH_TIMEOUT
        ):
            _LOGGER.warning(
                "Stove still heating %.0f s after turn-off; unlocking power control",
                OFF_LATCH_TIMEOUT,
            )
            self._off_latched_at = None
        return self._off_latched_at is None and is_running(phase, real_power)

    async def _send_setpoint(
        self, session: _Session, temperature: int, command: str | None = None
    ) -> None:
        """Send a setpoint; it stays queued (and retried) until a write succeeds."""
        self._setpoint, self._setpoint_pending = temperature, True
        await _write(session, command or setpoint_command(temperature))
        self._setpoint_pending = False

    async def _run(self, operation: Callable[[_Session], Awaitable[_T]]) -> _T:
        """Run one operation on a fresh connection, under the lock and time cap."""
        async with self._lock:
            self._deadline = asyncio.get_running_loop().time() + OPERATION_TIMEOUT
            try:
                async with asyncio.timeout(OPERATION_TIMEOUT):
                    return await self._run_connected(operation)
            except TimeoutError as err:
                raise DuepiTransportError(
                    f"Relay operation exceeded {OPERATION_TIMEOUT:.0f} s"
                ) from err

    async def _run_connected(self, operation: Callable[[_Session], Awaitable[_T]]) -> _T:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), CONNECT_TIMEOUT
            )
        except TimeoutError as err:
            raise DuepiTransportError(f"Timed out connecting to {self._host}") from err
        except OSError as err:
            raise DuepiConnectionError(
                f"Cannot connect to {self._host}:{self._port}: {err.strerror or type(err).__name__}"
            ) from None
        try:
            writer.write(f"master:{self._device_code}#".encode())
            await writer.drain()
            await asyncio.sleep(LOGIN_DELAY)
            await _drain(reader)
            return await operation(_Session(reader, writer, self._key))
        except (ConnectionError, OSError) as err:
            raise DuepiTransportError(
                f"Relay connection lost: {type(err).__name__}"
            ) from None
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
