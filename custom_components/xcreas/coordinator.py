"""The xCREAS data coordinator (docs/ha-integration.md §4/§6/§7).

Merge-pull: the push connection schedules refreshes on "changed" notices
(client-side debounce on top of the server throttle) and the low-frequency
fallback poll (FALLBACK_POLL_SECONDS) recovers silently lost notifications.

The snapshot is the AUTHORITATIVE device inventory (§4): a refresh succeeds
only on a strictly valid body (account_id, inventory_complete:true, unique
stable device ids, account match). Any failure keeps the previous device
list — entities/cleanup NEVER run on a failed or partial read. Device-set
changes are announced to the platforms (dynamic add/remove of the entities)
via listeners registered with async_add_device_listener.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import XcreasApiClient, XcreasAuthError, XcreasSnapshotInvalid, parse_snapshot
from .const import AVAILABILITY_ONLINE, FALLBACK_POLL_SECONDS

_LOGGER = logging.getLogger(__name__)


@dataclass
class XcreasDeviceData:
    """One device block of the last full snapshot (raw dict kept verbatim —
    the projections live in the entities so the contract stays single-sourced
    with docs/ha-integration.md §4)."""

    device_id: str
    raw: dict[str, Any]

    @property
    def name(self) -> str | None:
        name = self.raw.get("name")
        return name if isinstance(name, str) and name else None

    @property
    def model(self) -> str:
        return str(self.raw.get("model") or "V350s")

    @property
    def availability(self) -> str:
        return str(self.raw.get("availability") or AVAILABILITY_ONLINE)

    @property
    def online(self) -> bool:
        return self.availability == AVAILABILITY_ONLINE

    @property
    def supports_fan_level(self) -> bool:
        return self.raw.get("supports_fan_level") is True

    @property
    def control(self) -> dict[str, Any]:
        v = self.raw.get("control")
        return v if isinstance(v, dict) else {}

    @property
    def sensors(self) -> dict[str, Any]:
        v = self.raw.get("sensors")
        return v if isinstance(v, dict) else {}


@dataclass
class XcreasSnapshot:
    account_id: str
    devices: dict[str, XcreasDeviceData] = field(default_factory=dict)


class XcreasCoordinator(DataUpdateCoordinator[XcreasSnapshot]):
    """The account-level coordinator: one snapshot, N devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: XcreasApiClient,
        account_id: str,
        config_entry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"xcreas {account_id}",
            update_interval=timedelta(seconds=FALLBACK_POLL_SECONDS),
        )
        self._client = client
        self.account_id = account_id
        # Push plumbing (wired by async_setup_entry).
        self._ws = None
        self._push_refresh_task: asyncio.Task | None = None
        self._push_refresh_queued = False
        self._full_success_seen = False
        # Device-set listeners (the platforms' dynamic add/remove hooks).
        self._device_listeners: list[Callable[[XcreasSnapshot], None]] = []

    # --- snapshot refresh ---

    async def _async_update_data(self) -> XcreasSnapshot:
        try:
            body = await self._client.async_get_snapshot()
        except XcreasAuthError as err:
            # The canonical HA mechanism: ConfigEntryAuthFailed from a
            # coordinator update starts the reauth flow (dead grant/refresh
            # token must not linger as "unavailable" forever).
            raise ConfigEntryAuthFailed(f"xcreas auth failed: {err.code}") from err
        if not isinstance(body, dict) or not body.get("account_id"):
            raise UpdateFailed("snapshot missing account_id")
        if body.get("account_id") != self.account_id:
            # 账号不匹配 = 错误账号数据 — the snapshot is invalid for THIS
            # entry (V03: 错误账号数据不触发删除); keep the previous list.
            raise UpdateFailed(
                f"snapshot account {body.get('account_id')!r} does not match entry account {self.account_id!r}"
            )
        try:
            blocks = parse_snapshot(body)
        except XcreasSnapshotInvalid as err:
            # Strict completeness contract (§4): field missing, wrong type,
            # duplicate ids or a missing inventory_complete marker invalidate
            # the whole inventory — keep the last known-good list.
            raise UpdateFailed(f"snapshot failed strict validation: {err.args[0]}") from err
        snapshot = XcreasSnapshot(account_id=body["account_id"])
        for raw in blocks:
            snapshot.devices[raw["device_id"]] = XcreasDeviceData(raw["device_id"], raw)
        self._full_success_seen = True
        self._notify_device_listeners(snapshot)
        return snapshot

    # --- device-set listeners (dynamic entity add/remove) ---

    @callback
    def async_add_device_listener(
        self, listener: Callable[[XcreasSnapshot], None]
    ) -> Callable[[], None]:
        """Register a device-set listener; it fires on every successful
        inventory update (including the first refresh). Returns the unhook."""
        self._device_listeners.append(listener)

        @callback
        def _unhook() -> None:
            if listener in self._device_listeners:
                self._device_listeners.remove(listener)

        return _unhook

    @callback
    def _notify_device_listeners(self, snapshot: XcreasSnapshot) -> None:
        for listener in list(self._device_listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 — one broken platform never kills the refresh
                _LOGGER.exception("xcreas device listener failed")

    # --- push hooks (called from the websocket task) ---

    @callback
    def async_connection_up(self) -> None:
        """Subscribed: pull a snapshot right away (重连校准) and then rely on
        notices; the fallback poll stays armed regardless."""
        self._schedule_push_refresh()

    @callback
    def async_connection_down(self) -> None:
        """Disconnected: keep the last data; entities stay with their last
        real values (the availability property keeps judging per device)."""

    @callback
    def async_connection_changed(self) -> None:
        """One throttled "changed" notice → one coalesced snapshot pull."""
        self._schedule_push_refresh()

    def _schedule_push_refresh(self) -> None:
        """One in-flight push refresh at a time; a burst of notices (server
        throttle trailing edge) coalesces. A notice arriving DURING an
        in-flight refresh sets the queued flag — the trailing pull below
        re-reads, so the LAST change can never be swallowed (§3.2: 拉取期间
        收到新通知时合并为一次后续刷新，不能丢掉最后一次变更).

        NOTE: the refresh task method must NOT be named `_debounced_refresh` —
        DataUpdateCoordinator sets an instance attribute with that exact name
        (its request-refresh Debouncer), which would shadow the method and
        break every push refresh at runtime (found live, invisible to tests
        that bypass __init__).
        """
        if self._push_refresh_task is not None and not self._push_refresh_task.done():
            self._push_refresh_queued = True
            return
        self._push_refresh_queued = False
        # ConfigEntry.async_create_background_task (NOT hass.config_entries —
        # that object has no such method, found live; NOT a bare asyncio task —
        # this one is canceled automatically when the entry unloads). Verified
        # present in HA 2025.4.4 and 2026.10.4 sources.
        self._push_refresh_task = self.config_entry.async_create_background_task(
            self.hass, self._push_refresh_once(), name="xcreas-push-refresh"
        )

    async def _push_refresh_once(self) -> None:
        await asyncio.sleep(0)  # yield once so trailing notices can pile in
        while True:
            self._push_refresh_queued = False
            await self._async_refresh(log_failures=False)
            if not self._push_refresh_queued:
                return
            await asyncio.sleep(0)  # yield before the trailing pull

    @callback
    def async_connection_revoked(self) -> None:
        """The grant died server-side: start the re-auth flow (§7)."""
        _LOGGER.info("xcreas grant revoked — starting reauth")
        entry = self.config_entry
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                entry.domain,
                context={"source": "reauth", "entry_id": entry.entry_id},
                data=entry.data,
            )
        )

    @property
    def client(self) -> XcreasApiClient:
        return self._client

    # --- entity-facing helpers ---

    def device_data(self, device_id: str) -> "XcreasDeviceData | None":
        return self.data.devices.get(device_id) if self.data else None

    def device_name(self, device_id: str) -> str | None:
        dev = self.device_data(device_id)
        return dev.name if dev else None

    def device_model(self, device_id: str) -> str:
        dev = self.device_data(device_id)
        return dev.model if dev else "V350s"

    # --- lifecycle ---

    def attach_ws(self, ws) -> None:
        self._ws = ws

    async def shutdown(self) -> None:
        """Unload/reload: release the ws task, subscription and connection."""
        if self._push_refresh_task is not None and not self._push_refresh_task.done():
            self._push_refresh_task.cancel()
        if self._ws is not None:
            await self._ws.stop()
            self._ws = None

    @property
    def full_success_seen(self) -> bool:
        return self._full_success_seen



