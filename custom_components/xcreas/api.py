"""xCREAS cloud API client (docs/ha-integration.md §4/§5).

The client rides the integration's OAuth2 session (token refresh handled by
HA's `OAuth2Session`) and maps the documented machine error codes onto typed
exceptions. No secret ever lives here: the token is HA-managed.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import aiohttp

from .const import COMMAND_PATH, SNAPSHOT_PATH

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0
# Machine codes from docs/ha-integration.md §5/§6.
CODE_INVALID_TOKEN = "invalid_token"
CODE_INSUFFICIENT_SCOPE = "insufficient_scope"
CODE_DEVICE_NOT_AUTHORIZED = "device_not_authorized"
CODE_DEVICE_NOT_FOUND = "device_not_found"
CODE_DEVICE_OFFLINE = "device_offline"
CODE_DEVICE_UNAVAILABLE = "device_unavailable"
CODE_REQUEST_ID_CONFLICT = "request_id_conflict"
CODE_CAPABILITY = "capability_not_supported"
CODE_INVALID_PARAMS = "invalid_params"
CODE_INVALID_ACTION = "invalid_action"
CODE_SNAPSHOT_TOO_LARGE = "snapshot_too_large"
CODE_ACCESS_UNAVAILABLE = "device_access_unavailable"


class XcreasApiError(Exception):
    """A failed API exchange with the documented machine code."""

    def __init__(self, code: str, status: int | None = None, detail: str = "") -> None:
        super().__init__(f"{code} (status={status})" + (f": {detail}" if detail else ""))
        self.code = code
        self.status = status
        self.detail = detail


class XcreasAuthError(XcreasApiError):
    """The bearer token is dead — the entry must re-authenticate."""


class XcreasConnectionError(XcreasApiError):
    """Transport-level failure (timeout/network) — retryable."""


class XcreasSnapshotInvalid(Exception):
    """A 200 snapshot body that fails the strict completeness contract
    (docs/ha-integration.md §4): the response is NOT an authoritative
    inventory, so the receiver must keep its previous device list. `reason`
    is one of: missing_account_id, bad_account_id, missing_inventory_complete,
    bad_devices, bad_device_block, duplicate_device_id."""


class XcreasApiClient:
    """HA-side client for the xCREAS HA surface."""

    def __init__(
        self,
        websession: aiohttp.ClientSession,
        base_url: str,
        token_getter,
        *,
        request_id_prefix: str = "ha",
    ) -> None:
        self._websession = websession
        self._base_url = base_url.rstrip("/")
        self._token_getter = token_getter
        self._request_id_prefix = request_id_prefix

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _headers(self) -> dict[str, str]:
        token = await self._token_getter()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _machine_code(body: Any, status: int) -> str:
        # The envelope is {"code": <status>, "message": "<machine code>"} —
        # the message IS the machine code (docs/ha-integration.md §5).
        if isinstance(body, dict):
            message = body.get("message")
            if isinstance(message, str) and message:
                return message
        return "service_unavailable"

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                resp = await self._websession.request(
                    method,
                    self._url(path),
                    headers=await self._headers(),
                    **kwargs,
                )
        except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError) as err:
            raise XcreasConnectionError("transport_error", detail=str(err)) from err
        if resp.status == 204:
            return {}
        try:
            body = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            body = None
        if resp.status >= 400:
            code = self._machine_code(body, resp.status)
            err_cls = XcreasAuthError if resp.status in (401, 403) and code in (
                CODE_INVALID_TOKEN,
                CODE_INSUFFICIENT_SCOPE,
            ) else XcreasApiError
            raise err_cls(code, resp.status)
        return body

    async def async_get_snapshot(self) -> dict[str, Any]:
        """The authorized snapshot (§4): account + eligible devices."""
        return await self._request("GET", SNAPSHOT_PATH)

    async def async_send_command(
        self,
        device_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """One idempotent control command (§5)."""
        if request_id is None:
            request_id = self.new_request_id()
        payload: dict[str, Any] = {
            "device_id": device_id,
            "action": action,
            "request_id": request_id,
        }
        if params:
            payload["params"] = params
        return await self._request("POST", COMMAND_PATH, json=payload)

    def new_request_id(self) -> str:
        """An idempotency id matching the server charset (8..128 URL-safe)."""
        return f"{self._request_id_prefix}-{uuid.uuid4().hex[:16]}-{int(time.time())}"


def parse_snapshot(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and STRICTLY validate the device blocks of a snapshot body
    (docs/ha-integration.md §4: 账号 ID、完整性标记、devices 数组及其中的
    稳定设备 ID 严格校验；任何失败都使本次清单失效).

    Raises XcreasSnapshotInvalid (never silently drops devices — an
    incomplete or malformed inventory must not trigger entity cleanup).
    """
    if not isinstance(body, dict):
        raise XcreasSnapshotInvalid("missing_account_id")
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise XcreasSnapshotInvalid("missing_account_id")
    # inventory_complete:true is THE completeness marker (§4): it must appear
    # and be true — a response without it is not an authoritative inventory.
    if body.get("inventory_complete") is not True:
        raise XcreasSnapshotInvalid("missing_inventory_complete")
    devices = body.get("devices")
    if not isinstance(devices, list):
        raise XcreasSnapshotInvalid("bad_devices")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in devices:
        if not isinstance(d, dict):
            raise XcreasSnapshotInvalid("bad_device_block")
        device_id = d.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise XcreasSnapshotInvalid("bad_device_block")
        if device_id in seen:
            raise XcreasSnapshotInvalid("duplicate_device_id")
        seen.add(device_id)
        out.append(d)
    return out
