"""BLE transport for a Solity WELKOM lock with the Bluetooth pack.

Reproduces the Solity app's BLE handshake over Home Assistant's Bluetooth
proxy mesh, bypassing the cloud entirely:

    connect -> notify(48400003)
    0x10 EXCHANGE_KEY (plaintext)   -> lock replies 0x90 with a receive key
    session IV = appKey[0:8] + receiveKey[0:8]
    0x11 AUTH_APP   (AES-128-CBC)   -> lock replies 0x91, result 0 = authed
    0x21 OPEN / 0x22 CLOSE (AES-CBC)-> lock replies 0xA1 / 0xA2, result 0 = done

Right after auth the lock pushes a 0xD1 status frame (deadbolt, sub-latch,
battery, registered-credential counts, system mode). Every BLE session
captures it, so open/close/status all return a fresh ``SolityStatus``.

Frame: ``48 <len> <cmd> <data...> <checksum>`` with ``len = len(data)+1`` and
``checksum = (len + cmd + sum(data)) & 0xFF``. The AES key is fixed across
(non-China) locks; the per-lock secret is ``appKey`` — the owner account's
``regDeviceAppKey`` (base64).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WRITE_UUID = "48400002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_UUID = "48400003-b5a3-f393-e0a9-e50e24dcca9e"

AES_KEY = b"H-GANG BLE MODE1"  # fixed, non-China firmware
STX = 0x48

CMD_EXCHANGE_KEY = 0x10
CMD_AUTH_APP = 0x11
CMD_OPEN = 0x21
CMD_CLOSE = 0x22
CMD_STATUS_REQ = 0x51  # ask the lock for its status
ACK_EXCHANGE_KEY = 0x90
ACK_AUTH_APP = 0x91
ACK_OPEN = 0xA1
ACK_CLOSE = 0xA2
ACK_STATUS = 0xD1  # status reply (to 0x51), carries the DoorLockStatus payload
ACK_ERROR = 0xFF

# Standard manual-open parameters (app clickStateIcon, BLE path):
#   code = 0, address = 0, second = 7
OPEN_SUFFIX = bytes([0, 0, 7])
CLOSE_SUFFIX = bytes()

CONNECT_ATTEMPTS = 4
RECV_TIMEOUT = 6.0
CMD_RECV_TIMEOUT = 8.0
STATUS_DRAIN_TIMEOUT = 2.5


class SolityBleError(Exception):
    """A BLE command failed."""


@dataclass
class SolityStatus:
    """Parsed 0xD1 door-lock status frame."""

    deadbolt: int | None = None
    sub_latch: int | None = None
    battery: int | None = None
    pw_info: int | None = None
    count_card: int | None = None
    count_remote: int | None = None
    count_fingerprint: int | None = None
    count_mastercard: int | None = None
    system_mode: int | None = None
    count_face: int | None = None
    count_vein: int | None = None
    user_pw_info: bytes = field(default=b"")

    @property
    def is_locked(self) -> bool | None:
        """Real deadbolt state (1 = thrown/locked, 0 = retracted)."""
        if self.deadbolt is None:
            return None
        return self.deadbolt == 1


def _parse_status(data: bytes) -> SolityStatus:
    """Decode a DoorLockStatus payload (byte layout from the Solity app)."""

    def g(i: int) -> int | None:
        return data[i] if i < len(data) else None

    return SolityStatus(
        deadbolt=g(0),
        sub_latch=g(1),
        battery=g(2),
        pw_info=g(3),
        count_card=g(4),
        count_remote=g(5),
        count_fingerprint=g(6),
        count_mastercard=g(7),
        system_mode=g(8),
        count_face=g(9),
        count_vein=g(10),
        user_pw_info=bytes(data[11:]) if len(data) > 11 else b"",
    )


def parse_app_key(raw: str) -> bytes:
    """Decode the base64 ``regDeviceAppKey`` into its key bytes."""
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as err:  # noqa: BLE001
        raise SolityBleError(f"appKey is not valid base64: {err}") from err
    if len(key) < 8:
        raise SolityBleError(f"appKey too short ({len(key)} bytes, need >= 8)")
    return key


def _time_key() -> bytes:
    """7-byte current-time key: year(hi,lo), month, day, hour, minute, second."""
    n = datetime.now()
    return bytes(
        [(n.year >> 8) & 0xFF, n.year & 0xFF, n.month, n.day, n.hour, n.minute, n.second]
    )


def _build(cmd: int, data: bytes) -> bytes:
    length = (len(data) + 1) & 0xFF
    checksum = (length + cmd + sum(data)) & 0xFF
    return bytes([STX, length, cmd]) + data + bytes([checksum])


def _pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _encrypt(data: bytes, iv: bytes) -> bytes:
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    return enc.update(_pad(data)) + enc.finalize()


def _decrypt(data: bytes, iv: bytes) -> bytes:
    # App decrypts with NoPadding and reads the frame by its length field.
    dec = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


class SolityBleClient:
    """Sends OPEN / CLOSE / status reads to one lock over BLE via the proxy mesh."""

    def __init__(self, hass: HomeAssistant, mac: str, app_key: bytes) -> None:
        self.hass = hass
        self.mac = mac.upper()
        self.app_key = app_key
        self._lock = asyncio.Lock()

    async def async_open(self) -> SolityStatus | None:
        """Retract the deadbolt. Returns the status captured this session."""
        return await self._run(CMD_OPEN, OPEN_SUFFIX, ACK_OPEN)

    async def async_close(self) -> SolityStatus | None:
        """Throw the deadbolt. Returns the status captured this session."""
        return await self._run(CMD_CLOSE, CLOSE_SUFFIX, ACK_CLOSE)

    async def async_read_status(self) -> SolityStatus | None:
        """Connect, authenticate, request status (0x51), and parse 0xD1."""
        return await self._run(CMD_STATUS_REQ, b"", ACK_STATUS)

    async def _run(
        self, cmd: int | None, suffix: bytes | None, ack: int | None
    ) -> SolityStatus | None:
        async with self._lock:  # one BLE session at a time per lock
            return await self._session(cmd, suffix, ack)

    async def _session(
        self, cmd: int | None, suffix: bytes | None, ack: int | None
    ) -> SolityStatus | None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac, connectable=True
        )
        if ble_device is None:
            raise SolityBleError(
                f"{self.mac} not reachable by any Bluetooth proxy right now"
            )

        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_notify(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        status: SolityStatus | None = None

        def _note(frame: bytes) -> None:
            nonlocal status
            if len(frame) >= 3 and frame[0] == STX and frame[2] == ACK_STATUS:
                _LOGGER.debug("Solity BLE 0xD1 status frame: %s", frame.hex())
                status = _parse_status(frame[3 : 3 + (frame[1] - 1)])

        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.mac,
            max_attempts=CONNECT_ATTEMPTS,
        )
        try:
            await client.start_notify(NOTIFY_UUID, _on_notify)

            async def _await(want: int, iv: bytes | None, timeout: float) -> bytes:
                while True:
                    raw = await asyncio.wait_for(queue.get(), timeout)
                    frame = raw if iv is None else _decrypt(raw, iv)
                    if len(frame) < 3 or frame[0] != STX:
                        continue
                    if frame[2] == ACK_ERROR:
                        raise SolityBleError("lock reported receive error (0xFF)")
                    _note(frame)
                    if frame[2] == want:
                        return frame

            # 1) EXCHANGE_KEY (plaintext) -> 0x90 with the receive key
            await client.write_gatt_char(
                WRITE_UUID,
                _build(CMD_EXCHANGE_KEY, _time_key() + self.app_key),
                response=True,
            )
            exch = await _await(ACK_EXCHANGE_KEY, None, RECV_TIMEOUT)
            receive_key = exch[3 : 3 + (exch[1] - 1)]
            if len(receive_key) < 8:
                raise SolityBleError(f"short receiveKey ({len(receive_key)}B)")

            # 2) session IV = appKey[0:8] + receiveKey[0:8]
            iv = (bytes(self.app_key[:8]) + bytes(receive_key[:8]) + bytes(16))[:16]

            # 3) AUTH_APP (encrypted) -> 0x91 result 0
            await client.write_gatt_char(
                WRITE_UUID,
                _encrypt(_build(CMD_AUTH_APP, _time_key() + self.app_key), iv),
                response=True,
            )
            auth = await _await(ACK_AUTH_APP, iv, RECV_TIMEOUT)
            if auth[3] != 0:
                raise SolityBleError(f"auth rejected (result={auth[3]})")

            # 4) optional OPEN / CLOSE (encrypted) -> 0xA1 / 0xA2 result 0
            if cmd is not None:
                cmd_data = _time_key() + self.app_key + (suffix or b"")
                await client.write_gatt_char(
                    WRITE_UUID, _encrypt(_build(cmd, cmd_data), iv), response=True
                )
                done = await _await(ack, iv, CMD_RECV_TIMEOUT)
                # OPEN/CLOSE reply with a result byte; the 0xD1 status reply
                # is the payload itself (captured by _note), not a result.
                if cmd in (CMD_OPEN, CMD_CLOSE) and done[3] != 0:
                    raise SolityBleError(
                        f"command 0x{cmd:02X} rejected (result={done[3]})"
                    )

            # 5) drain remaining frames briefly to capture the pushed 0xD1 status
            with contextlib.suppress(TimeoutError):
                while True:
                    raw = await asyncio.wait_for(queue.get(), STATUS_DRAIN_TIMEOUT)
                    frame = _decrypt(raw, iv)
                    if len(frame) >= 3 and frame[0] == STX:
                        if frame[2] == ACK_ERROR:
                            continue
                        _note(frame)

            if cmd is not None:
                _LOGGER.debug("Solity BLE 0x%02X OK (%s)", cmd, self.mac)
            return status
        except TimeoutError as err:
            raise SolityBleError("timed out waiting for lock response") from err
        except SolityBleError:
            raise
        except Exception as err:  # noqa: BLE001
            raise SolityBleError(f"BLE error: {err}") from err
        finally:
            try:
                await client.stop_notify(NOTIFY_UUID)
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
