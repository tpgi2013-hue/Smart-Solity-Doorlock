"""Async client for the Smart Solity cloud API.

Reverse-engineered from the Smart Solity Android app (kr.co.h_gang.smartsolity).
Auth model: POST /api_v2/login returns a JWT ``token`` (30-day) plus a
secondary ``tokenPwd``. Both are sent on every authed call as the
``Authorization`` and ``AuthorizationPwd`` headers. ``lang`` must be the
numeric code ``"0"`` (Korean) — the string ``"ko"`` yields a half-token.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.smartsolity.com"
PHONE_TOKEN = "ha-solity"  # a stable, HA-dedicated device id (own session)
TIMEOUT_SECONDS = 15
TIMEOUT = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
# Tokens last ~30 days. A 401 well before that means another login with the
# same account (e.g. the phone app) took the session over: the server keeps
# only one session per account.
TOKEN_FRESH_SECONDS = 25 * 24 * 3600


def hash_password(password: str) -> str:
    """Return base64(sha256(password)) — the app's ``hashedPwd``."""
    return base64.b64encode(hashlib.sha256(password.encode()).digest()).decode()


class SolityError(Exception):
    """Generic Solity API error."""


class SolityAuthError(SolityError):
    """Authentication failed (bad credentials / expired session)."""


def _conn_error(err: Exception) -> SolityError:
    """Readable error: aiohttp timeouts stringify to ''."""
    if isinstance(err, asyncio.TimeoutError):
        return SolityError(f"no response from the lock (timed out after {TIMEOUT_SECONDS}s)")
    return SolityError(f"connection error: {err}")


class SolityClient:
    """Minimal async client with automatic token refresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        hashed_pwd: str,
    ) -> None:
        self._session = session
        self._email = email
        self._hashed = hashed_pwd
        self._token: str | None = None
        self._token_pwd: str | None = None
        self._token_at: float | None = None
        self._lock = asyncio.Lock()
        # Called when another login (same account) kicked this session out.
        self.on_session_taken: Callable[[], None] | None = None

    async def _raw(
        self,
        method: str,
        path: str,
        body: dict | None,
        token: str | None,
        token_pwd: str | None,
    ) -> tuple[int, str]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        if token_pwd:
            headers["AuthorizationPwd"] = token_pwd
        async with self._session.request(
            method, f"{BASE_URL}{path}", json=body, headers=headers, timeout=TIMEOUT
        ) as resp:
            return resp.status, await resp.text()

    async def login(self) -> None:
        """Authenticate and cache the token pair."""
        async with self._lock:
            body = {
                "emailId": self._email,
                "hashedPwd": self._hashed,
                "phoneToken": PHONE_TOKEN,
                "appSource": "0",
                "lang": "0",
            }
            try:
                status, text = await self._raw("POST", "/api_v2/login", body, None, None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise _conn_error(err) from err

            try:
                data = json.loads(text)
            except ValueError as err:
                raise SolityError(f"bad login response ({status})") from err

            contents = data.get("contents") or {}
            if contents.get("loginResult") != 0 or not contents.get("token"):
                msg = contents.get("loginMessage") or f"loginResult={contents.get('loginResult')}"
                raise SolityAuthError(msg)

            self._token = contents["token"]
            self._token_pwd = contents.get("tokenPwd")
            self._token_at = time.monotonic()
            _LOGGER.debug("Solity login OK")

    async def _authed(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self._token:
            await self.login()
        try:
            status, text = await self._raw(method, path, body, self._token, self._token_pwd)
            if status in (401, 403):
                _LOGGER.debug("Solity %s -> %s, re-login", path, status)
                taken = (
                    self._token_at is not None
                    and time.monotonic() - self._token_at < TOKEN_FRESH_SECONDS
                )
                await self.login()
                if taken:
                    _LOGGER.warning(
                        "Smart Solity session was taken over by another login with "
                        "the same account (e.g. the phone app); logged in again. "
                        "Use a separate (invited member) account for Home Assistant"
                    )
                    if self.on_session_taken:
                        self.on_session_taken()
                status, text = await self._raw(method, path, body, self._token, self._token_pwd)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise _conn_error(err) from err

        if status in (401, 403):
            raise SolityAuthError(f"HTTP {status}")
        if status >= 400:
            raise SolityError(f"HTTP {status}: {text[:200]}")
        try:
            return json.loads(text)
        except ValueError as err:
            raise SolityError("bad response body") from err

    async def get_devices(self) -> list[dict]:
        """Return the door locks on this account, owned or shared.

        Owned locks come in ``myDeviceList``; locks shared with a member
        account may come in another list, so every list of device-like dicts
        under ``contents`` is collected.
        """
        data = await self._authed("GET", "/api_v2/myDevice")
        contents = data.get("contents") or {}
        devices: list[dict] = []
        seen: set[str] = set()
        for key, value in contents.items():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not item.get("myDeviceId"):
                    continue
                if item["myDeviceId"] in seen:
                    continue
                seen.add(item["myDeviceId"])
                devices.append({**item, "_list": key})
        if not devices:
            _LOGGER.warning(
                "Smart Solity: no door lock found on this account; response "
                "contents keys: %s",
                {k: type(v).__name__ for k, v in contents.items()},
            )
        return devices

    async def control(self, device_id: str, control_type: str, option: str = "1") -> dict:
        """Send a control command (open / close / get_status / ...)."""
        return await self._authed(
            "PUT",
            f"/api_v2/controlDevice/{device_id}",
            {"controlType": control_type, "optionValue": option},
        )

    async def get_status(self, device_id: str) -> dict:
        """Return parsed lock status (deadBolt, battery, counts, ...)."""
        data = await self.control(device_id, "get_status", "")
        msg = (data.get("contents") or {}).get("controlDeviceMessage", "{}")
        try:
            status = json.loads(msg)
        except (ValueError, TypeError):
            return {}
        return status if isinstance(status, dict) else {}

    async def retrieve_log(
        self,
        device_id: str,
        length: int = 20,
        start: int = 0,
        member_id: str = "",
        log_type: str = "",
        timezone: str = "+9:00",
    ) -> list[dict]:
        """Return recent access-log entries (cloud-stored, does NOT wake the lock).

        Each entry has logDateTime, logType, logCode, mediaType, nickname,
        logMessage, etc. Ordered newest-first by the server.
        """
        query = urlencode(
            {
                "pMemberId": member_id,
                "pLogType": log_type,
                "pLogStart": start,
                "pLogLength": length,
                "pTimezone": timezone,
            }
        )
        data = await self._authed(
            "GET", f"/api_v2/retrieveLog/page/{device_id}?{query}"
        )
        return ((data.get("contents") or {}).get("retrieveLogList")) or []

    async def open(self, device_id: str) -> dict:
        """Unlock the door."""
        return await self.control(device_id, "open", "1")

    async def close(self, device_id: str) -> dict:
        """Lock the door."""
        return await self.control(device_id, "close", "1")
