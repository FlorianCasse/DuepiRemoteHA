"""Tests for the Duepi relay transport (frames, mapping, connection handling)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest

# Dummy code used everywhere: never a real device code.
CODE = "0123456789"
OTHER_CODE = "zyxwv98765"

# Register values read from a stopped stove on 2026-11-06.
OFF_REGISTERS = {
    "RD9000": 0,
    "RD3000": 1,
    "RD4000": 0,
    "RD1000": 0xCE,
    "RD0000": 0x0F,
    "RDA000": 0,
}
# Stable heating as read at 13:31 the same day: setting P5, running P5.
HEATING_REGISTERS = {**OFF_REGISTERS, "RD9000": 0x0201, "RD3000": 5, "RD4000": 5, "RD0000": 172}
POLL = ["RD9000", "RD3000", "RD4000", "RD1000", "RD0000", "RDA000"]


@pytest.fixture
def relay(duepi_test_modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    """Relay module with test-speed timings."""
    module = duepi_test_modules.relay
    monkeypatch.setattr(module, "LOGIN_DELAY", 0)
    # Tests drive a fresh client at once; the startup guard has its own tests.
    monkeypatch.setattr(module, "STARTUP_GUARD", 0)
    return module


@pytest.fixture(autouse=True)
def _device_code_never_logged(caplog: pytest.LogCaptureFixture):
    """protects=the device code never reaches any log line in any relay test
    (spec AC6); fails_when=a log call formats the code or the login line;
    no_claim=Home Assistant's own logging of entry data."""
    caplog.set_level(logging.DEBUG)
    yield
    # caplog.text only holds the current (teardown) phase: read the test's records.
    messages = [
        record.getMessage() for phase in ("setup", "call") for record in caplog.get_records(phase)
    ]
    for code in (CODE, OTHER_CODE):
        assert not any(code in message for message in messages)


class FakeRelay:
    """Minimal asyncio relay server answering like a Duepi module."""

    def __init__(self, relay, code: str = CODE) -> None:
        self.relay = relay
        self.key = relay.derive_key(code)
        self.registers = dict(OFF_REGISTERS)
        self.logins: list[bytes] = []
        self.commands: list[str] = []
        self.connections = 0
        self.reply_delay = 0.0
        self.corrupt_checksum = False
        # Cleartext frame the real relay sometimes sends before a reply
        # (observed during ignition, 2026-11-06): ESC 00010021 &
        self.cleartext_prefix = b""
        self.close_after: int | None = None
        # Reply without decoding requests, like a module holding another code.
        self.blind = False
        # Per-command misbehaviour: corrupt the reply, or never reply.
        self.corrupt_commands: set[str] = set()
        self.silent_commands: set[str] = set()
        # Raw bytes sent right after the login, before any request.
        self.greeting = b""
        # Raw bytes sent instead of every reply (malformed relay output).
        self.raw_reply: bytes | None = None
        # Called with each command after it is recorded (simulate the stove changing).
        self.on_command = None
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def __aenter__(self) -> FakeRelay:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_args: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            self.logins.append(await reader.readuntil(b"#"))
            if self.greeting:
                writer.write(self.greeting)
                await writer.drain()
            while True:
                frame = await reader.readuntil(b"&")
                if self.blind:
                    command = "RD9000"
                else:
                    plaintext = self.relay.decode_frame(self.key, frame)
                    command = plaintext[:6]
                    assert plaintext[6:] == self.relay.checksum(command)
                self.commands.append(command)
                if self.on_command is not None:
                    self.on_command(command)
                if self.close_after is not None and len(self.commands) > self.close_after:
                    return
                await asyncio.sleep(self.reply_delay)
                if command in self.silent_commands:
                    continue
                if self.raw_reply is not None:
                    writer.write(self.raw_reply)
                    await writer.drain()
                    continue
                data = f"{self.registers.get(command, 0):04X}00"
                corrupt = self.corrupt_checksum or command in self.corrupt_commands
                check = "00" if corrupt else self.relay.checksum(data)
                writer.write(self.cleartext_prefix + self._encrypt(data + check))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def encrypted_reply(self, value: int) -> bytes:
        data = f"{value:04X}00"
        return self._encrypt(data + self.relay.checksum(data))

    def _encrypt(self, plaintext: str) -> bytes:
        # Replies use the same framing as requests; reuse the encoder's AES path.
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        import base64
        import os

        iv = os.urandom(16)
        padded = plaintext.encode().ljust(16, b"\0")
        enc = Cipher(algorithms.AES(self.key), modes.CBC(iv)).encryptor()
        return b"\x1b" + base64.b64encode(iv + enc.update(padded) + enc.finalize()) + b"&"


def run(coro):
    return asyncio.run(coro)


# --- Frame codec ---------------------------------------------------------


def test_known_answer_vector_matches_spec(relay) -> None:
    """protects=wire format compatible with the module; fails_when=key derivation,
    checksum, padding or framing changes; no_claim=acceptance by a real module."""
    key = relay.derive_key(CODE)
    assert key.hex() == "5be4823cf94bd2e58553ec201fa25ec6"
    frame = relay.encode_frame(key, "RD9000", iv=bytes(16))
    assert frame == b"\x1bAAAAAAAAAAAAAAAAAAAAALLifCC1HkBdciXX+5IoTxY=&"
    assert relay.decode_frame(key, frame) == "RD90005F"


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [("00000020", 0), ("00CE0048", 0xCE), ("000F0036", 0x0F)],
)
def test_parse_reply_reads_values_from_the_probe(relay, plaintext: str, expected: int) -> None:
    """protects=value decoding of real replies; fails_when=value slice or checksum
    rule changes; no_claim=none beyond protects."""
    assert relay.parse_reply(plaintext) == expected


@pytest.mark.parametrize("plaintext", ["00000021", "0000002", "000000200", "ZZZZ00F4"])
def test_parse_reply_rejects_corrupt_replies(relay, plaintext: str) -> None:
    """protects=corrupt replies never become state; fails_when=checksum or length
    check removed; no_claim=real relay error formats."""
    with pytest.raises(relay.DuepiProtocolError):
        relay.parse_reply(plaintext)


def test_decode_frame_rejects_garbage(relay) -> None:
    key = relay.derive_key(CODE)
    for frame in (b"\x1bnot-base64!&", b"\x1bAAAA&"):
        with pytest.raises(relay.DuepiProtocolError):
            relay.decode_frame(key, frame)


@pytest.mark.parametrize(
    "command", ["RF0000", "RF0010", "RF0030", "RF0050", "RF2150", "RF2FF0"]
)
def test_allowed_writes(relay, command: str) -> None:
    """protects=every command the client needs stays sendable (off, P1-P5,
    setpoints); fails_when=the allow-list is narrowed by mistake; no_claim=none."""
    assert relay.validate_write(command) == command


@pytest.mark.parametrize(
    "command",
    ["RD9000", "RC5000", "RF0060", "RF3000", "RF00100", "RF2150 ", "rf0010", "RF2G50", ""],
)
def test_write_allow_list_rejects_everything_else(relay, command: str) -> None:
    """protects=only known safe writes can be sent; fails_when=the allow-list is
    widened; no_claim=none beyond protects."""
    with pytest.raises(ValueError):
        relay.validate_write(command)


def test_device_id_is_stable_and_hides_the_code(relay) -> None:
    device_id = relay.relay_device_id(CODE)
    assert device_id == relay.relay_device_id(CODE)
    assert device_id.startswith("relay_") and len(device_id) == 14
    assert CODE not in device_id
    assert device_id != relay.relay_device_id(OTHER_CODE)


# --- State mapping -------------------------------------------------------


def test_stopped_stove_mapping(relay) -> None:
    """protects=a stopped stove reads as off with real values; fails_when=scaling
    or zero handling changes; no_claim=phase codes other than 0 (Task 0)."""
    state = relay.state_from_registers(OFF_REGISTERS)
    assert state.power_on is False
    assert state.flame_on is False
    assert state.status_text == "Off"
    assert state.room_temperature == pytest.approx(20.6)
    assert state.fumes_temperature == 15.0
    assert state.working_power == 1  # RD3000: the setting survives while off
    assert state.power_setting == 1
    assert state.real_power == 0
    assert state.set_temperature is None
    assert state.alarm is None
    assert state.online is True and state.raw_online is True


def test_unknown_phase_is_on_but_not_running(relay) -> None:
    """protects=fail-safe handling of unmapped phases; fails_when=unknown phases
    get a running flag or read as off; no_claim=real phase meanings."""
    registers = {**OFF_REGISTERS, "RD9000": 7, "RD3000": 3, "RD4000": 2, "RDA000": 5}
    state = relay.state_from_registers(registers, set_temperature=21)
    assert state.power_on is True
    assert state.status_text == "Phase 7"
    assert state.flame_on is None
    assert state.phase == 7
    assert state.working_power == 3 and state.real_power == 2
    assert state.set_temperature == 21
    assert state.alarm == "E5" and state.error_code == 5
    assert 7 not in relay.PHASES


# --- Client against a fake relay ------------------------------------------


def test_poll_logs_in_reads_six_registers_and_closes(relay) -> None:
    """protects=per-operation connection and login; fails_when=connection reused,
    login missing or register order changes; no_claim=real relay session limits."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = relay.DuepiRelayClient("127.0.0.1", server.port, CODE)
            first = await client.async_get_stove_state()
            second = await client.async_get_stove_state()
            return server, first, second

    server, first, second = run(scenario())
    assert server.connections == 2
    assert server.logins == [f"master:{CODE}#".encode()] * 2
    assert server.commands == POLL * 2
    assert first == second
    assert first.room_temperature == pytest.approx(20.6)


def test_unsolicited_cleartext_frames_are_skipped(relay) -> None:
    """protects=polling keeps working when the relay interleaves cleartext frames
    (seen on a real stove during ignition); fails_when=the first frame is taken as
    the reply or cleartext is trusted as state; no_claim=meaning of those frames."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(RD9000=0x0102)
            server.cleartext_prefix = b"\x1b00010021&"
            client = relay.DuepiRelayClient("127.0.0.1", server.port, CODE)
            return await client.async_get_stove_state()

    state = run(scenario())
    assert state.phase == 0x0102
    assert state.room_temperature == pytest.approx(20.6)


def test_read_register_rejects_writes(relay) -> None:
    """protects=the read helper cannot be used to send a write; fails_when=the
    read-command check is removed; no_claim=none beyond protects."""
    client = relay.DuepiRelayClient("127.0.0.1", 1, CODE)
    with pytest.raises(ValueError):
        run(client.async_read_register("RF0010"))


def test_wrong_checksum_from_relay_raises_protocol_error(relay) -> None:
    async def scenario():
        async with FakeRelay(relay) as server:
            server.corrupt_checksum = True
            client = relay.DuepiRelayClient("127.0.0.1", server.port, CODE)
            await client.async_get_stove_state()

    with pytest.raises(relay.DuepiProtocolError):
        run(scenario())


def test_wrong_device_code_raises_protocol_error(relay) -> None:
    """protects=a wrong code is detectable (config flow maps it to invalid_auth);
    fails_when=undecryptable replies are accepted; no_claim=real relay behaviour
    for unknown codes."""

    async def scenario():
        async with FakeRelay(relay, code=OTHER_CODE) as server:
            server.blind = True
            client = relay.DuepiRelayClient("127.0.0.1", server.port, CODE)
            return await client.async_read_register("RD9000")

    with pytest.raises(relay.DuepiProtocolError) as err:
        run(scenario())
    assert CODE not in str(err.value)


def test_connection_lost_mid_poll_then_next_poll_recovers(relay) -> None:
    """protects=a dropped connection leaves no state behind; fails_when=a closed
    connection is reused; no_claim=none beyond protects."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = relay.DuepiRelayClient("127.0.0.1", server.port, CODE)
            server.close_after = 2
            with pytest.raises(relay.DuepiTransportError):
                await client.async_get_stove_state()
            server.close_after = None
            return await client.async_get_stove_state()

    assert run(scenario()).power_on is False


def test_connection_refused_is_a_connection_error(relay) -> None:
    async def scenario():
        server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        client = relay.DuepiRelayClient("127.0.0.1", port, CODE)
        await client.async_get_stove_state()

    with pytest.raises(relay.DuepiConnectionError) as err:
        run(scenario())
    assert CODE not in str(err.value)


def test_global_cap_stops_a_slow_poll_and_frees_the_lock(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=user commands are not blocked by a slow relay (R5/C9);
    fails_when=the global cap is removed or the lock is not released; every
    reply arrives within its frame timeout, so only the global cap can fail it;
    no_claim=real network latency."""
    monkeypatch.setattr(relay, "OPERATION_TIMEOUT", 0.5)
    monkeypatch.setattr(relay, "FRAME_TIMEOUT", 0.3)

    async def scenario():
        async with FakeRelay(relay) as server:
            server.reply_delay = 0.15  # 6 reads ≈ 0.9 s > 0.5 s cap
            client = relay.DuepiRelayClient("127.0.0.1", server.port, CODE)
            poll = asyncio.create_task(client.async_get_stove_state())
            await asyncio.sleep(0.05)
            queued = asyncio.create_task(client.async_read_register("RD9000"))
            with pytest.raises(relay.DuepiTransportError):
                await poll
            return await queued

    assert run(scenario()) == 0


# --- Config flow and setup -------------------------------------------------


def _relay_input(port: int, code: str = CODE) -> dict[str, object]:
    return {"host": "127.0.0.1", "port": port, "device_code": f" {code} "}


def test_user_step_offers_cloud_and_relay(duepi_test_modules: SimpleNamespace) -> None:
    flow = duepi_test_modules.config_flow.DuepiConfigFlow()
    result = run(flow.async_step_user())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["cloud", "relay"]


def test_relay_step_creates_an_entry_with_a_derived_device_id(
    duepi_test_modules: SimpleNamespace, relay
) -> None:
    """protects=relay entries carry CONF_DEVICE_ID, which every platform reads (C7);
    fails_when=device_id is missing or is the raw code; no_claim=Home Assistant's
    real flow manager (stubbed)."""
    flow = duepi_test_modules.config_flow.DuepiConfigFlow()

    async def scenario():
        async with FakeRelay(relay) as server:
            return server, await flow.async_step_relay(_relay_input(server.port))

    server, result = run(scenario())
    device_id = relay.relay_device_id(CODE)
    assert result["type"] == "create_entry"
    assert result["data"] == {
        "transport": "relay",
        "host": "127.0.0.1",
        "port": server.port,
        "device_code": CODE,
        "device_id": device_id,
    }
    assert flow._unique_id == device_id
    assert CODE not in result["title"]
    assert server.commands == ["RD9000"]


def test_relay_step_reports_a_wrong_code(duepi_test_modules: SimpleNamespace, relay) -> None:
    """protects=a wrong device code shows relay_invalid_code; fails_when=decrypt
    failures map to another error; no_claim=real relay behaviour for unknown codes."""
    flow = duepi_test_modules.config_flow.DuepiConfigFlow()

    async def scenario():
        async with FakeRelay(relay, code=OTHER_CODE) as server:
            server.blind = True
            return await flow.async_step_relay(_relay_input(server.port))

    result = run(scenario())
    assert result["type"] == "form"
    assert result["errors"] == {"base": "relay_invalid_code"}


def test_relay_step_reports_an_unreachable_relay(
    duepi_test_modules: SimpleNamespace, relay
) -> None:
    """protects=a closed port shows relay_cannot_connect; fails_when=connection
    errors map to another key; no_claim=DNS failures."""
    async def closed_port() -> int:
        server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        return port

    flow = duepi_test_modules.config_flow.DuepiConfigFlow()
    result = run(flow.async_step_relay(_relay_input(run(closed_port()))))
    assert result["errors"] == {"base": "relay_cannot_connect"}


def test_setup_entry_builds_a_relay_client_without_cloud_login(
    duepi_test_modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=relay entries load without dpremoteiot credentials; fails_when=setup
    reads email/password or builds a cloud client for a relay entry; no_claim=real
    platform setup (coordinator stubbed)."""
    import importlib

    duepi_init = importlib.import_module("custom_components.duepi.__init__")
    created: dict[str, object] = {}

    class FakeCoordinator:
        def __init__(self, *args: object, **_kwargs: object) -> None:
            created["client"] = args[2]

        async def async_config_entry_first_refresh(self) -> None:
            return None

    def no_session(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("relay entries must not create a cloud session")

    monkeypatch.setattr(duepi_init, "DuepiCoordinator", FakeCoordinator)
    monkeypatch.setattr(duepi_init, "async_create_clientsession", no_session)
    entry = SimpleNamespace(
        data={
            "transport": "relay",
            "host": "duepi.example.invalid",
            "port": 3000,
            "device_code": CODE,
            "device_id": "relay_00000000",
        },
        options={},
        async_on_unload=lambda _callback: None,
        add_update_listener=lambda _listener: object(),
    )

    async def forward(_entry: object, _platforms: object) -> None:
        return None

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_forward_entry_setups=forward))
    assert run(duepi_init.async_setup_entry(hass, entry)) is True
    client = created["client"]
    assert isinstance(client, duepi_test_modules.relay.DuepiRelayClient)
    assert client.device_id == duepi_test_modules.relay.relay_device_id(CODE)


def test_diagnostics_redact_the_relay_entry(duepi_test_modules: SimpleNamespace) -> None:
    """protects=the device code and relay host never reach diagnostics (C1);
    fails_when=relay keys are dropped from redaction; no_claim=HA core storage."""
    import importlib
    import json

    diagnostics = importlib.import_module("custom_components.duepi.diagnostics")
    relay_module = duepi_test_modules.relay
    entry = SimpleNamespace(
        data={
            "transport": "relay",
            "host": "duepiwebserver9.example.invalid",
            "port": 3000,
            "device_code": CODE,
            "device_id": relay_module.relay_device_id(CODE),
        },
        options={},
        runtime_data=SimpleNamespace(
            data=relay_module.state_from_registers(OFF_REGISTERS),
            client=relay_module.DuepiRelayClient("h", 1, CODE),
            raw_online=True,
            filtered_online=True,
            disconnect_elapsed_seconds=None,
            last_update_success=True,
            last_successful_update_time=None,
        ),
    )
    result = run(diagnostics.async_get_config_entry_diagnostics(object(), entry))
    serialized = json.dumps(result, default=str)
    assert CODE not in serialized
    assert "duepiwebserver9" not in serialized
    assert result["state"]["real_power"] == 0


# --- Commands and safety guards (T2: R1, R6, D15, D16) -----------------------


def _writes(server: FakeRelay) -> list[str]:
    return [command for command in server.commands if command.startswith("RF")]


def _client(relay, server: FakeRelay, **kwargs: object):
    return relay.DuepiRelayClient("127.0.0.1", server.port, CODE, **kwargs)


def test_turn_on_ignites_a_stopped_stove_without_sending_the_setpoint(relay) -> None:
    """protects=ignition sends only the power command, the setpoint follows once
    heating (D15); fails_when=a setpoint reaches a stopped stove or the order
    changes; no_claim=the module's ignition timing (fake relay)."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server)
            await client.async_turn_on(power=2, temperature=21, ignite=True)
            after_ignition = list(_writes(server))
            ignition_state = await client.async_get_stove_state()  # still off
            server.registers.update(HEATING_REGISTERS)
            heating_state = await client.async_get_stove_state()
            again = await client.async_get_stove_state()
            return server, after_ignition, ignition_state, heating_state, again

    server, after_ignition, ignition_state, heating_state, again = run(scenario())
    assert after_ignition == ["RF0020"]
    assert ignition_state.set_temperature == 21
    assert _writes(server) == ["RF0020", "RF2150"]  # 21 C sent once, after heating
    assert heating_state.set_temperature == 21 and again.set_temperature == 21


def test_only_an_explicit_turn_on_may_ignite(relay) -> None:
    """protects=presets never start a stopped stove (R1); fails_when=ignite=False
    sends a power command to a stopped stove; no_claim=none beyond protects."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server)
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_turn_on(power=1, temperature=18, ignite=False)
            return server

    assert _writes(run(scenario())) == []


@pytest.mark.parametrize(
    ("registers", "refused"),
    [
        ({"RD9000": 0x0102, "RD4000": 0}, False),  # ignition already running
        ({"RD9000": 0x0401, "RD4000": 2}, True),  # brazier cleaning
        ({"RD9000": 0x0300, "RD4000": 2}, True),  # unknown phase (e.g. shutdown)
    ],
)
def test_turn_on_never_sends_power_outside_off_or_heating(relay, registers, refused) -> None:
    """protects=no power command lands on a stove that is starting, cleaning or in
    an unknown phase, and a refused start is reported instead of silently dropped;
    fails_when=ignite=True sends RF00p0 whenever not heating or returns success
    while shutting down; no_claim=real shutdown phase codes."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(registers)
            client = _client(relay, server)
            if refused:
                with pytest.raises(relay.DuepiStartRefusedError):
                    await client.async_turn_on(power=3, temperature=22, ignite=True)
            else:
                await client.async_turn_on(power=3, temperature=22, ignite=True)
            return server

    assert _writes(run(scenario())) == []


@pytest.mark.parametrize(
    ("registers", "allowed"),
    [
        ({"RD9000": 0x0201, "RD4000": 3}, True),
        ({"RD9000": 0x0201, "RD4000": 0}, False),  # heating phase but no real power
        ({"RD9000": 0x0401, "RD4000": 2}, False),  # cleaning
        ({"RD9000": 0x0101, "RD4000": 1}, False),  # ignition
        ({"RD9000": 0, "RD4000": 0}, False),  # off
        ({"RD9000": 0x0999, "RD4000": 3}, False),  # unknown
    ],
)
def test_set_power_needs_heating_phase_and_real_power(relay, registers, allowed) -> None:
    """protects=a power change can never ignite (R6 double guard); fails_when=either
    check is dropped or read outside the write's connection; no_claim=the
    sub-second race with the panel (documented)."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(registers)
            client = _client(relay, server)
            if allowed:
                await client.async_set_power(4)
            else:
                with pytest.raises(relay.DuepiStoveOffError):
                    await client.async_set_power(4)
            return server

    server = run(scenario())
    assert _writes(server) == (["RF0040"] if allowed else [])
    assert server.connections == 1  # guard reads and write share one connection
    assert server.commands[:2] == ["RD9000", "RD4000"]


def test_preset_on_a_heating_stove_sends_power_then_setpoint(relay) -> None:
    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            client = _client(relay, server)
            await client.async_turn_on(power=1, temperature=18, ignite=False)
            state = await client.async_get_stove_state()
            return server, state

    server, state = run(scenario())
    assert _writes(server) == ["RF0010", "RF2120"]
    assert state.set_temperature == 18


def test_setpoint_set_while_stopped_waits_for_an_ha_start(relay) -> None:
    """protects=no setpoint reaches a stopped stove (D15), HA shows the last value
    it set (D16), and a stove later started from its panel keeps its own setpoint
    (ship red team); fails_when=RF2xx0 is sent while off or after a panel start;
    no_claim=whether the stove applied it (not readable)."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server, default_temperature=25)
            first = await client.async_get_stove_state()
            await client.async_set_temperature(19)
            while_off = await client.async_get_stove_state()
            server.registers.update(HEATING_REGISTERS)  # started from the panel
            await client.async_get_stove_state()
            writes_after_panel_start = list(_writes(server))
            await client.async_set_temperature(20)  # heating: sent at once
            return server, first, while_off, writes_after_panel_start

    server, first, while_off, writes_after_panel_start = run(scenario())
    assert first.set_temperature == 25  # seeded from the default option
    assert while_off.set_temperature == 19
    assert writes_after_panel_start == []
    assert _writes(server) == ["RF2140"]


def test_turn_off_sends_off_and_drops_the_queued_setpoint(relay) -> None:
    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server)
            await client.async_set_temperature(22)  # queued: stove off
            await client.async_turn_off()
            server.registers.update(HEATING_REGISTERS)
            await client.async_get_stove_state()
            return server

    assert _writes(run(scenario())) == ["RF0000"]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_power", (0,)),
        ("async_set_power", (6,)),
        ("async_set_temperature", (4,)),
        ("async_set_temperature", (41,)),
    ],
)
def test_out_of_range_commands_fail_before_any_connection(relay, method, args) -> None:
    client = relay.DuepiRelayClient("127.0.0.1", 1, CODE)
    with pytest.raises(ValueError):
        run(getattr(client, method)(*args))


def test_turn_on_requires_a_power_level(relay) -> None:
    client = relay.DuepiRelayClient("127.0.0.1", 1, CODE)
    with pytest.raises(ValueError):
        run(client.async_turn_on(temperature=21, ignite=True))


def test_relay_client_conforms_to_the_protocol(duepi_test_modules: SimpleNamespace) -> None:
    """protects=the relay client is interchangeable with the cloud client (R4);
    fails_when=a protocol member or parameter is missing; no_claim=behaviour."""
    import inspect

    api = duepi_test_modules.api
    client = duepi_test_modules.relay.DuepiRelayClient
    for name in ("transport_name", "reports_real_power"):
        assert name in api.DuepiClient.__annotations__ and hasattr(client, name)
    for name, expected in vars(api.DuepiClient).items():
        if name.startswith("_") or name in {"transport_name", "reports_real_power"}:
            continue
        actual = getattr(client, name)
        if isinstance(expected, property):
            assert isinstance(actual, property), name
            continue
        assert list(inspect.signature(actual).parameters) == list(
            inspect.signature(expected).parameters
        ), name


# --- Review fixes (2026-11-06): ignition safety, setpoint robustness, input ---


@pytest.mark.parametrize("payload", [b"garbage&", b"A" * 600])
def test_malformed_relay_output_is_a_transport_error_not_a_wrong_code(
    duepi_test_modules: SimpleNamespace, relay, payload
) -> None:
    """protects=junk from the relay is bounded and reported as unreachable, not as
    a wrong device code; fails_when=the frame-start or size guard is removed or
    framing errors are mapped to invalid code; no_claim=real relay error text."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.raw_reply = payload
            client = _client(relay, server)
            with pytest.raises(relay.DuepiTransportError) as err:
                await client.async_read_register("RD9000")
            flow = duepi_test_modules.config_flow.DuepiConfigFlow()
            result = await flow.async_step_relay(_relay_input(server.port))
            return err.value, result

    error, result = run(scenario())
    assert not isinstance(error, relay.DuepiProtocolError)
    assert result["errors"] == {"base": "relay_cannot_connect"}


@pytest.mark.parametrize("code", ["", "ab#cd", "12 34", "abc", "x" * 33, "ab:cd"])
def test_relay_step_rejects_malformed_device_codes(
    duepi_test_modules: SimpleNamespace, code
) -> None:
    """protects=codes that would corrupt the login line never reach the relay;
    fails_when=the format check is removed or loosened; no_claim=real label formats."""
    flow = duepi_test_modules.config_flow.DuepiConfigFlow()
    result = run(flow.async_step_relay({"host": "127.0.0.1", "port": 1, "device_code": code}))
    assert result["type"] == "form"
    assert result["errors"] == {"device_code": "invalid_device_code"}


def test_power_is_locked_after_turn_off_until_the_stove_leaves_heating(relay) -> None:
    """protects=a power change or preset right after turn-off cannot cancel the
    shutdown (state-machine review); fails_when=the latch is missing or never
    released; no_claim=how long a real stove keeps reporting 513."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            client = _client(relay, server)
            await client.async_turn_off()
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_set_power(3)
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_turn_on(power=1, temperature=18, ignite=False)
            await client.async_get_stove_state()  # still 513: latch kept
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_set_power(3)
            server.registers.update(RD9000=0x0801, RD4000=0)  # shutting down
            await client.async_get_stove_state()
            server.registers.update(HEATING_REGISTERS)  # restarted later
            await client.async_set_power(3)
            return server

    assert _writes(run(scenario())) == ["RF0000", "RF0030"]


@pytest.mark.parametrize(
    "registers",
    [{"RDA000": 5}, {"RD4000": 2}],
)
def test_ignition_needs_no_alarm_and_no_running_power(relay, registers) -> None:
    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(registers)
            with pytest.raises(relay.DuepiStartRefusedError):
                await _client(relay, server).async_turn_on(power=2, temperature=21, ignite=True)
            return server

    assert _writes(run(scenario())) == []


def test_second_ignition_is_refused_for_a_minute_even_if_the_reply_was_lost(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=a retried turn-on cannot send a second ignition while the first may
    be in progress (adversarial F1); fails_when=the attempt is recorded only after
    a successful reply or the cooldown is removed; no_claim=module timing."""
    monkeypatch.setattr(relay, "FRAME_TIMEOUT", 0.3)

    async def scenario():
        async with FakeRelay(relay) as server:
            server.silent_commands = {"RF0020"}
            client = _client(relay, server)
            with pytest.raises(relay.DuepiTransportError):
                await client.async_turn_on(power=2, temperature=21, ignite=True)
            with pytest.raises(relay.DuepiStartRefusedError):
                await client.async_turn_on(power=2, temperature=21, ignite=True)
            return server

    assert _writes(run(scenario())) == ["RF0020"]


def test_frames_received_before_the_first_request_are_discarded(relay) -> None:
    """protects=a stray reply left by an earlier session cannot be read as the
    phase (adversarial F2); fails_when=the post-login drain is removed;
    no_claim=whether the real relay forwards late replies."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            server.greeting = server.encrypted_reply(0)  # stale "0" frame
            return await _client(relay, server).async_read_register("RD9000")

    assert run(scenario()) == 0x0201


def test_failed_setpoint_write_stays_queued_and_is_retried(relay) -> None:
    """protects=a setpoint that never reached the stove is not shown as applied
    and is sent again (state-machine + adversarial F5); fails_when=the setpoint is
    marked sent before the write succeeds; no_claim=stove acceptance."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            server.corrupt_commands = {"RF2150"}
            client = _client(relay, server)
            with pytest.raises(relay.DuepiProtocolError):
                await client.async_set_temperature(21)
            server.corrupt_commands = set()
            state = await client.async_get_stove_state()
            return server, state

    server, state = run(scenario())
    assert _writes(server) == ["RF2150", "RF2150"]
    assert state.set_temperature == 21


def test_failed_queued_setpoint_does_not_break_the_poll(relay) -> None:
    """protects=the stove stays available when the queued setpoint write fails
    (testing + adversarial F6); fails_when=the write error propagates out of the
    poll; no_claim=how often the real module rejects a setpoint."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server)
            await client.async_turn_on(power=2, temperature=22, ignite=True)  # queued
            server.registers.update(HEATING_REGISTERS)
            server.corrupt_commands = {"RF2160"}
            failing_poll = await client.async_get_stove_state()
            server.corrupt_commands = set()
            await client.async_get_stove_state()
            again = await client.async_get_stove_state()
            return server, failing_poll, again

    server, failing_poll, again = run(scenario())
    assert failing_poll.phase == 0x0201
    assert _writes(server) == ["RF0020", "RF2160", "RF2160"]
    assert again.set_temperature == 22


PHASE_CASES = {
    "off": {"RD9000": 0, "RD4000": 0},
    "ignition": {"RD9000": 0x0102, "RD4000": 0},
    "cleaning": {"RD9000": 0x0401, "RD4000": 2},
    "heating_no_power": {"RD9000": 0x0201, "RD4000": 0},
    "unknown": {"RD9000": 0x0300, "RD4000": 1},
    "heating": {"RD9000": 0x0201, "RD4000": 3},
}


@pytest.mark.parametrize("phase", sorted(PHASE_CASES))
def test_phase_action_matrix(relay, phase) -> None:
    """protects=every phase x action cell (state-machine review): turn_off always
    sends RF0000; setpoint and preset only write in stable heating;
    fails_when=a guard admits a write in a non-heating phase; no_claim=real codes
    beyond those observed on one stove."""
    heating = phase == "heating"

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(PHASE_CASES[phase])
            client = _client(relay, server)
            await client.async_set_temperature(20)
            if heating:
                await client.async_turn_on(power=1, temperature=18, ignite=False)
            else:
                with pytest.raises(relay.DuepiStoveOffError):
                    await client.async_turn_on(power=1, temperature=18, ignite=False)
            await client.async_get_stove_state()
            await client.async_turn_off()
            return server

    writes = _writes(run(scenario()))
    if heating:
        assert writes == ["RF2140", "RF0010", "RF2120", "RF0000"]
    else:
        assert writes == ["RF0000"]


# --- Second review pass (2026-11-06) ----------------------------------------


def test_power_stays_locked_when_the_turn_off_reply_is_lost(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=a shutdown whose acknowledgement was lost cannot be cancelled by a
    power command (re-review F1); fails_when=the latch is set only after the
    RF0000 reply; no_claim=whether the module acted on the lost command."""
    monkeypatch.setattr(relay, "FRAME_TIMEOUT", 0.3)

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            server.silent_commands = {"RF0000"}
            client = _client(relay, server)
            with pytest.raises(relay.DuepiTransportError):
                await client.async_turn_off()
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_set_power(3)
            return server

    assert _writes(run(scenario())) == ["RF0000"]


def test_turn_off_latch_expires_when_the_stove_keeps_heating(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=a stove that ignored the turn-off is controllable again after the
    latch timeout (re-review F2, 10 min in production); fails_when=the latch never
    expires; no_claim=why the stove kept heating."""
    monkeypatch.setattr(relay, "OFF_LATCH_TIMEOUT", 0.2)

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            client = _client(relay, server)
            await client.async_turn_off()
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_set_power(3)
            await asyncio.sleep(0.3)
            await client.async_set_power(3)
            return server

    assert _writes(run(scenario())) == ["RF0000", "RF0030"]


def test_queued_setpoint_is_skipped_when_the_poll_is_short_of_time(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=the queued setpoint write never pushes a poll past the global cap
    (re-review F4); fails_when=the write is attempted with less than a frame
    timeout left; no_claim=real relay latency."""
    monkeypatch.setattr(relay, "OPERATION_TIMEOUT", 1.0)
    monkeypatch.setattr(relay, "FRAME_TIMEOUT", 0.5)

    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server)
            await client.async_turn_on(power=2, temperature=22, ignite=True)  # queued
            server.registers.update(HEATING_REGISTERS)
            server.reply_delay = 0.1  # 6 reads ~0.6 s of the 1 s budget
            state = await client.async_get_stove_state()
            return server, state

    server, state = run(scenario())
    assert state.phase == 0x0201
    assert _writes(server) == ["RF0020"]


def test_shutdown_phase_reads_as_off(relay) -> None:
    """protects=the climate entity shows off while the stove cools down after a
    turn-off (phase 0x0801 observed 2026-11-06); fails_when=shutdown is treated as
    an unknown, powered-on phase; no_claim=other shutdown sub-codes beyond 0x08xx."""
    for phase in (0x0801, 0x0802):
        state = relay.state_from_registers({**OFF_REGISTERS, "RD9000": phase, "RD0000": 155})
        assert state.power_on is False
        assert state.flame_on is False
        assert state.status_text == "Shutdown"
    assert not relay.is_running(0x0801, 1)



# --- Coverage audit gaps (ship, 2026-11-06) -----------------------------------


def test_hung_connect_is_a_retryable_transport_error(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=a relay that never accepts the connection yields a retryable
    DuepiTransportError; fails_when=the TimeoutError clause is removed or placed
    after OSError (TimeoutError is an OSError); no_claim=real network timing."""

    async def hang(*_args: object, **_kwargs: object):
        await asyncio.sleep(10)

    monkeypatch.setattr(relay, "CONNECT_TIMEOUT", 0.1)
    monkeypatch.setattr(relay.asyncio, "open_connection", hang)
    client = relay.DuepiRelayClient("127.0.0.1", 1, CODE)
    with pytest.raises(relay.DuepiTransportError) as err:
        run(client.async_read_register("RD9000"))
    assert CODE not in str(err.value)


def test_connection_reset_mid_exchange_is_a_transport_error(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=a reset during an exchange becomes DuepiTransportError without the
    device code; fails_when=the ConnectionError/OSError wrapper is removed;
    no_claim=real reset timing (exchange patched)."""

    async def reset(self, command: str) -> str:
        raise ConnectionResetError(f"reset during {command}")

    async def scenario():
        async with FakeRelay(relay) as server:
            monkeypatch.setattr(relay._Session, "exchange", reset)
            await _client(relay, server).async_read_register("RD9000")

    with pytest.raises(relay.DuepiTransportError) as err:
        run(scenario())
    assert not isinstance(err.value, relay.DuepiProtocolError)
    assert CODE not in str(err.value)


def test_duplicate_relay_entry_aborts_before_any_relay_traffic(
    duepi_test_modules: SimpleNamespace, relay
) -> None:
    """protects=adding the same device code twice aborts before contacting the
    relay; fails_when=the unique-id check is removed or moved after validation;
    no_claim=Home Assistant's flow manager (abort stubbed to raise)."""

    class AlreadyConfigured(Exception):
        pass

    flow = duepi_test_modules.config_flow.DuepiConfigFlow()

    def abort_if_configured() -> None:
        if flow._unique_id == relay.relay_device_id(CODE):
            raise AlreadyConfigured

    flow._abort_if_unique_id_configured = abort_if_configured

    async def scenario():
        async with FakeRelay(relay) as server:
            with pytest.raises(AlreadyConfigured):
                await flow.async_step_relay(_relay_input(server.port))
            return server

    assert run(scenario()).connections == 0


def test_unexpected_validation_error_shows_unknown(
    duepi_test_modules: SimpleNamespace, relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=an unexpected error during relay validation shows the "unknown"
    form error instead of crashing the flow; fails_when=the broad except is
    removed or remapped; no_claim=logging output content."""

    async def boom(self, register: str) -> int:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(relay.DuepiRelayClient, "async_read_register", boom)
    flow = duepi_test_modules.config_flow.DuepiConfigFlow()
    result = run(flow.async_step_relay(_relay_input(1)))
    assert result["errors"] == {"base": "unknown"}



# --- Ship review fixes (2026-11-06): reload guard, fresh checks, bounded wait --


def test_fresh_client_refuses_power_and_ignition_during_startup_guard(
    relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=a reload right after a turn-off or a start cannot bypass the lost
    latch and cooldown (Codex C1); fails_when=a new client writes at once;
    no_claim=persistence across restarts (none: the guard is time-based)."""
    monkeypatch.setattr(relay, "STARTUP_GUARD", 0.3)

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            client = _client(relay, server)
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_set_power(3)
            server.registers.update(OFF_REGISTERS)
            with pytest.raises(relay.DuepiStartRefusedError):
                await client.async_turn_on(power=2, temperature=21, ignite=True)
            await asyncio.sleep(0.35)
            server.registers.update(HEATING_REGISTERS)
            await client.async_set_power(3)
            return server

    assert _writes(run(scenario())) == ["RF0030"]


def test_queued_setpoint_rechecks_the_phase_right_before_writing(relay) -> None:
    """protects=a queued setpoint never reaches a stove that stopped during the
    poll's other reads (Codex C3); fails_when=the write relies on the phase read
    at the start of the poll; no_claim=sub-second races inside one exchange."""

    async def scenario():
        async with FakeRelay(relay) as server:
            client = _client(relay, server)
            await client.async_turn_on(power=2, temperature=22, ignite=True)
            server.registers.update(HEATING_REGISTERS)

            def stop_after_last_poll_read(command: str) -> None:
                if command == "RDA000":
                    server.registers.update(OFF_REGISTERS)

            server.on_command = stop_after_last_poll_read
            await client.async_get_stove_state()
            return server

    assert _writes(run(scenario())) == ["RF0020"]


def test_turn_off_latch_survives_a_cleaning_phase(relay) -> None:
    """protects=a cleaning cycle right after turn-off does not unlock power
    (ship specialist); fails_when=the latch is released on any non-heating
    phase; no_claim=real cleaning timing."""

    async def scenario():
        async with FakeRelay(relay) as server:
            server.registers.update(HEATING_REGISTERS)
            client = _client(relay, server)
            await client.async_turn_off()
            server.registers.update(RD9000=0x0401)  # brazier cleaning
            await client.async_get_stove_state()
            server.registers.update(HEATING_REGISTERS)
            with pytest.raises(relay.DuepiStoveOffError):
                await client.async_set_power(3)
            return server

    assert _writes(run(scenario())) == ["RF0000"]


def test_waiting_for_the_relay_is_bounded(relay, monkeypatch: pytest.MonkeyPatch) -> None:
    """protects=commands do not pile up without limit behind a slow operation, so
    a turn-off is not delayed for minutes (Codex C2); fails_when=the lock is
    awaited without a deadline; no_claim=real relay latency."""
    monkeypatch.setattr(relay, "LOCK_WAIT", 0.2)

    async def scenario():
        async with FakeRelay(relay) as server:
            server.reply_delay = 0.15  # one poll holds the lock ~0.9 s
            client = _client(relay, server)
            poll = asyncio.create_task(client.async_get_stove_state())
            await asyncio.sleep(0.05)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(relay.DuepiTransportError):
                await client.async_read_register("RD9000")
            waited = loop.time() - started
            await poll
            return waited

    assert run(scenario()) < 0.6


def test_relay_device_page_does_not_link_to_the_cloud(
    duepi_test_modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=relay users are not sent to dpremoteiot.com from the device page
    (ship red team); fails_when=configuration_url is set for relay devices;
    no_claim=how the HA frontend renders the link."""
    import importlib
    import sys
    from types import ModuleType

    if "homeassistant.helpers.device_registry" not in sys.modules:
        registry = ModuleType("homeassistant.helpers.device_registry")
        registry.DeviceInfo = dict
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", registry)
    device = importlib.import_module("custom_components.duepi.device")
    relay_info = device.build_device_info(duepi_test_modules.relay.relay_device_id(CODE))
    cloud_info = device.build_device_info("0123456789abcdef01234567")
    assert "configuration_url" not in relay_info
    assert cloud_info["configuration_url"] == "https://dpremoteiot.com/dashboard"
