"""The /ws/ha push connection (docs/ha-integration.md §7).

Merge-pull model: the server coalesces ingest events into throttled
"changed" notices; this client answers each notice by scheduling one
snapshot refresh on the coordinator (never an incremental merge). The
connection is isolated per grant — only selected devices notify; a
"revoked" frame or an auth-failed close must end the entry (re-auth).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import WS_BACKOFF_MAX, WS_BACKOFF_MIN, WS_PATH
from .api import CODE_INVALID_TOKEN

if TYPE_CHECKING:
    from .coordinator import XcreasCoordinator

_LOGGER = logging.getLogger(__name__)

FRAME_SUBSCRIBE = "subscribe"
FRAME_SUBSCRIBED = "subscribed"
FRAME_CHANGED = "changed"
FRAME_REVOKED = "revoked"
FRAME_ERROR = "error"


class XcreasRevoked(Exception):
    """The grant was revoked server-side (revoked frame or auth close)."""


class XcreasWebsocket:
    """One long-lived push connection with reconnect backoff + jitter."""

    def __init__(
        self,
        coordinator: XcreasCoordinator,
        websession: aiohttp.ClientSession,
        ws_url: str,
        token_getter,
    ) -> None:
        self._coordinator = coordinator
        self._websession = websession
        self._ws_url = ws_url
        self._token_getter = token_getter
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientWebSocketResponse | None = None
        self._subscribed = asyncio.Event()
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closing = True
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — shutdown path must never raise
                pass
            self._task = None

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            started = time.monotonic()
            try:
                await self._connect_and_read()
                attempt = 0  # a clean read cycle reset the backoff ladder
            except XcreasRevoked:
                _LOGGER.info("xcreas connection revoked — stopping the entry")
                self._coordinator.async_connection_revoked()
                return
            except ConfigEntryAuthFailed as err:
                # The token refresh died (revoked grant / dead refresh token):
                # HA's OAuth2Session raises ConfigEntryAuthFailed — route to
                # the reauth flow instead of retrying against a dead grant.
                _LOGGER.info("xcreas token refresh failed (%s) — stopping the entry", err)
                self._coordinator.async_connection_revoked()
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — reconnect loop owns recovery
                # WARNING (not debug): a silently failing push connection is
                # indistinguishable from "no state updates" for the user —
                # the fallback poll then hides it for minutes.
                _LOGGER.warning("xcreas ws cycle ended: %s", err)
            if self._closing:
                return
            attempt += 1
            base = min(WS_BACKOFF_MIN * (2 ** (attempt - 1)), WS_BACKOFF_MAX)
            jitter = base * random.uniform(-0.2, 0.2)
            delay = max(0.5, base + jitter)
            _LOGGER.warning("xcreas ws reconnecting in %.1fs (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)
            _ = started

    async def _connect_and_read(self) -> None:
        token = await self._token_getter()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            conn = await self._websession.ws_connect(
                self._ws_url,
                headers=headers,
                heartbeat=30,  # aiohttp ping/keepalive (server pongs at 60s bound)
            )
        except aiohttp.ClientResponseError as err:
            # Handshake rejected by the server/proxy — surface the HTTP status
            # (401 token, 503 service off, 4xx/5xx from a proxy) at WARNING.
            if err.status in (401, 403):
                raise XcreasRevoked(f"ws handshake {err.status}") from err
            raise ConnectionError(f"ws handshake rejected: HTTP {err.status}") from err
        except aiohttp.ClientError as err:
            raise ConnectionError(f"ws connect failed: {err}") from err
        _LOGGER.info("xcreas ws connected to %s", self._ws_url)
        self._session = conn
        try:
            await conn.send_json({"type": FRAME_SUBSCRIBE})
            async with asyncio.timeout(10):
                first = await conn.receive()
            if first.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"handshake frame lost: {first.type}")
            payload = first.json()
            if payload.get("type") == FRAME_ERROR:
                code = payload.get("code", "")
                if code == CODE_INVALID_TOKEN:
                    raise XcreasRevoked(payload)
                raise ConnectionError(f"subscribe refused: {payload}")
            if payload.get("type") != FRAME_SUBSCRIBED:
                raise ConnectionError(f"unexpected handshake frame: {payload}")
            _LOGGER.info(
                "xcreas ws subscribed account=%s devices=%s",
                payload.get("account_id"),
                payload.get("devices"),
            )
            self._coordinator.async_connection_up()

            async for msg in conn:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                    continue
                payload = msg.json()
                frame_type = payload.get("type")
                if frame_type == FRAME_CHANGED:
                    _LOGGER.debug("xcreas ws changed notice received")
                    self._coordinator.async_connection_changed()
                elif frame_type == FRAME_REVOKED:
                    raise XcreasRevoked(payload)
                elif frame_type == FRAME_ERROR:
                    _LOGGER.warning("xcreas ws error frame: %s", payload)
        finally:
            self._session = None
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 — close best-effort
                pass
            self._coordinator.async_connection_down()
