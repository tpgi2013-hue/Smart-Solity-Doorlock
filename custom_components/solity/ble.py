"""Direct BLE unlock path for Smart Solity locks (WELKOM + Bluetooth pack).

The cloud ``open`` relays through the WiFi+ module and the Solity server, which
adds seconds of latency — too slow to open the door *while walking in*. This
module talks to the lock's Bluetooth pack directly over BLE (through Home
Assistant's Bluetooth proxy mesh), reproducing the Solity app's handshake:

    connect -> notify(48400003)
    0x10 EXCHANGE_KEY (plaintext)   -> lock replies 0x90 with a receive key
    derive session IV = appKey[0:8] + receiveKey[0:8]
    0x11 AUTH_APP   (AES-128-CBC)   -> lock replies 0x91, result 0 = authed
    0x21 OPEN       (AES-128-CBC)   -> lock replies 0xA1, result 0 = opened

Frame: ``48 <len> <cmd> <data...> <checksum>`` where ``len = data+1`` and
``checksum = (len + cmd + sum(data)) & 0xFF``. The AES key is fixed across
(non-China) locks; the per-lock secret is ``appKey`` (the server's
``regDeviceAppKey``, base64), supplied via config options.
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

SERVICE_UUID = "48400001-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_UUID = "48400002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_UUID = "48400003-b5a3-f393-e0a9-e50e24dcca9e"

AES_KEY = b"H-GANG BLE MODE1"  # fixed, non-China firmware
STX = 0x48

CMD_EXCHANGE_KEY = 0x10
CMD_AUTH_APP = 0x11
CMD_OPEN = 0x21
ACK_EXCHANGE_KEY = 0x90
ACK_AUTH_APP = 0x91
ACK_OPEN = 0xA1
ACK_ERROR = 0xFF

# Standard manual-open parameters (app clickStateIcon, BLE path):
#   code = 0, address = 0, second = 7
OPEN_CODE = 0
OPEN_ADDRESS = 0
OPEN_SECOND = 7

CONNECT_ATTEMPTS = 4
RECV_TIMEOUT = 6.0
OPEN_RECV_TIMEOUT = 8.0


class SolityBleError(Exception):
    """A BLE unlock attempt failed."""


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


def parse_app_key(raw: str) -> bytes:
    """Decode the base64 ``regDeviceAppKey`` into its key bytes."""
    key = base64.b64decode(raw)
    if not key:
        raise SolityBleError("empty appKey")
    return key


class SolityBleClient:
    """Opens one lock over BLE via the HA Bluetooth proxy mesh."""

    def __init__(self, hass: HomeAssistant, mac: str, app_key: bytes) -> None:
        self.hass = hass
        self.mac = mac.upper()
        self.app_key = app_key

    async def async_open(self) -> None:
        """Run the full handshake and send OPEN. Raises SolityBleError on failure."""
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

            async def _await(cmd: int, iv: bytes | None, timeout: float) -> bytes:
                """Return the plaintext frame whose cmd matches, skipping others."""
                while True:
                    raw = await asyncio.wait_for(queue.get(), timeout)
                    frame = raw if iv is None else _decrypt(raw, iv)
                    if len(frame) < 3 or frame[0] != STX:
                        continue
                    if frame[2] == ACK_ERROR:
                        raise SolityBleError("lock reported receive error (0xFF)")
                    if frame[2] == cmd:
                        return frame

            # 1) EXCHANGE_KEY (plaintext) -> 0x90 carrying the receive key
            await client.write_gatt_char(
                WRITE_UUID, _build(CMD_EXCHANGE_KEY, _time_key() + self.app_key),
                response=True,
            )
            ack = await _await(ACK_EXCHANGE_KEY, None, RECV_TIMEOUT)
            data_len = ack[1] - 1
            receive_key = ack[3 : 3 + data_len]
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

            # 4) OPEN (encrypted) -> 0xA1 result 0
            open_data = _time_key() + self.app_key + bytes(
                [OPEN_CODE, OPEN_ADDRESS, OPEN_SECOND]
            )
            await client.write_gatt_char(
                WRITE_UUID, _encrypt(_build(CMD_OPEN, open_data), iv), response=True
            )
            opened = await _await(ACK_OPEN, iv, OPEN_RECV_TIMEOUT)
            if opened[3] != 0:
                raise SolityBleError(f"open rejected (result={opened[3]})")
            _LOGGER.debug("Solity BLE open OK (%s)", self.mac)
        except TimeoutError as err:
            raise SolityBleError("timed out waiting for lock response") from err
        except SolityBleError:
            raise
        except Exception as err:  # noqa: BLE001 - surface any bleak/proxy error
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
