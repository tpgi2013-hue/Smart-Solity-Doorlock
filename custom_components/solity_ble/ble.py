"""BLE transport for a Solity WELKOM lock with the Bluetooth pack.

Reproduces the Solity app's BLE handshake over Home Assistant's Bluetooth
proxy mesh, bypassing the cloud entirely:

    connect -> notify(48400003)
    0x10 EXCHANGE_KEY (plaintext)   -> lock replies 0x90 with a receive key
    session IV = appKey[0:8] + receiveKey[0:8]
    0x11 AUTH_APP   (AES-128-CBC)   -> lock replies 0x91, result 0 = authed
    0x21 OPEN / 0x22 CLOSE (AES-CBC)-> lock replies 0xA1 / 0xA2, result 0 = done

Frame: ``48 <len> <cmd> <data...> <checksum>`` with ``len = len(data)+1`` and
``checksum = (len + cmd + sum(data)) & 0xFF``. The AES key is fixed across
(non-China) locks; the per-lock secret is ``appKey`` — the owner account's
``regDeviceAppKey`` (base64), supplied via the config entry.
"""
from __future__ import annotations

import asyncio
import base64
import logging
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
ACK_EXCHANGE_KEY = 0x90
ACK_AUTH_APP = 0x91
ACK_OPEN = 0xA1
ACK_CLOSE = 0xA2
ACK_ERROR = 0xFF

# Standard manual-open parameters (app clickStateIcon, BLE path):
#   code = 0, address = 0, second = 7
OPEN_SUFFIX = bytes([0, 0, 7])
CLOSE_SUFFIX = bytes()

CONNECT_ATTEMPTS = 4
RECV_TIMEOUT = 6.0
CMD_RECV_TIMEOUT = 8.0


class SolityBleError(Exception):
    """A BLE command failed."""


def parse_app_key(raw: str) -> bytes:
    """Decode the base64 ``regDeviceAppKey`` into its key bytes."""
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, Exception) as err:  # noqa: BLE001
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
    """Sends OPEN / CLOSE to one lock over BLE via the HA proxy mesh."""

    def __init__(self, hass: HomeAssistant, mac: str, app_key: bytes) -> None:
        self.hass = hass
        self.mac = mac.upper()
        self.app_key = app_key
        self._lock = asyncio.Lock()

    async def async_open(self) -> None:
        """Retract the deadbolt. Raises SolityBleError on failure."""
        await self._run(CMD_OPEN, OPEN_SUFFIX, ACK_OPEN)

    async def async_close(self) -> None:
        """Throw the deadbolt. Raises SolityBleError on failure."""
        await self._run(CMD_CLOSE, CLOSE_SUFFIX, ACK_CLOSE)

    async def _run(self, cmd: int, suffix: bytes, ack: int) -> None:
        # One BLE session at a time per lock.
        async with self._lock:
            await self._handshake_and_command(cmd, suffix, ack)

    async def _handshake_and_command(self, cmd: int, suffix: bytes, ack: int) -> None:
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

            # 4) OPEN / CLOSE (encrypted) -> 0xA1 / 0xA2 result 0
            cmd_data = _time_key() + self.app_key + suffix
            await client.write_gatt_char(
                WRITE_UUID, _encrypt(_build(cmd, cmd_data), iv), response=True
            )
            done = await _await(ack, iv, CMD_RECV_TIMEOUT)
            if done[3] != 0:
                raise SolityBleError(f"command 0x{cmd:02X} rejected (result={done[3]})")
            _LOGGER.debug("Solity BLE 0x%02X OK (%s)", cmd, self.mac)
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
