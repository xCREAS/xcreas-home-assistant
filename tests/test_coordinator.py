"""Coordinator tests: strict snapshot validation, account matching, merge-pull
with the trailing pull, revoked → reauth, and the device-set listener
announcement (docs/ha-integration.md §4/§7, account-sync contract)."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.xcreas.coordinator import XcreasCoordinator, XcreasSnapshot

from .conftest import make_client


def test_snapshot_parse(contract) -> None:
    body = contract["snapshot"]
    snapshot = XcreasSnapshot(account_id=body["account_id"])
    from custom_components.xcreas.api import parse_snapshot

    for raw in parse_snapshot(body):
        snapshot.devices[raw["device_id"]] = None
    assert list(snapshot.devices) == ["V350s-001"]


def test_parse_snapshot_zero_devices_is_valid(contract) -> None:
    """§4: a COMPLETE empty inventory is valid (zero-device accounts)."""
    from custom_components.xcreas.api import parse_snapshot

    assert parse_snapshot(contract["snapshot_empty"]) == []


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b.pop("inventory_complete"), "missing_inventory_complete"),
        (lambda b: b.__setitem__("inventory_complete", False), "missing_inventory_complete"),
        (lambda b: b.__setitem__("devices", "nope"), "bad_devices"),
    ],
)
def test_parse_snapshot_strict_rejections(contract, mutate, reason) -> None:
    from custom_components.xcreas.api import XcreasSnapshotInvalid, parse_snapshot

    body = deepcopy(contract["snapshot"])
    mutate(body)
    with pytest.raises(XcreasSnapshotInvalid) as err:
        parse_snapshot(body)
    assert err.value.args[0] == reason


def test_device_data_accessors(contract) -> None:
    from custom_components.xcreas.coordinator import XcreasDeviceData

    dev = XcreasDeviceData("V350s-001", contract["snapshot"]["devices"][0])
    assert dev.name == "客厅净化器"
    assert dev.model == "V350s"
    assert dev.online is True
    assert dev.availability == "online"

    dev2 = XcreasDeviceData("V350s-002", contract["snapshot_offline_missing"]["devices"][0])
    assert dev2.name is None  # null name → HA default naming
    assert dev2.online is False
    assert dev2.control == {"power": dev2.control["power"]}
    assert dev2.sensors == {}


def _make_coordinator(client, account_id, contract) -> XcreasCoordinator:
    """A real-enough coordinator for _async_update_data tests (no hass)."""
    coordinator = XcreasCoordinator.__new__(XcreasCoordinator)
    coordinator._client = client
    coordinator.account_id = account_id
    coordinator._full_success_seen = False
    coordinator._device_listeners = []
    coordinator._push_refresh_task = None
    coordinator._push_refresh_queued = False
    _ = contract
    return coordinator


async def test_update_rejects_incomplete_snapshot(contract) -> None:
    """§4: a 200 body without inventory_complete invalidates the whole
    inventory — UpdateFailed, keep the previous list, no cleanup."""
    body = deepcopy(contract["snapshot"])
    body.pop("inventory_complete")
    client = make_client({})
    client.async_get_snapshot = lambda: _async_return(body)
    coordinator = _make_coordinator(client, body["account_id"], contract)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator._full_success_seen is False


async def test_update_rejects_account_mismatch(contract) -> None:
    """§4: 错误账号数据不触发删除 — a snapshot for another account is not
    this entry's inventory."""
    body = deepcopy(contract["snapshot"])
    body["account_id"] = "other-account"
    client = make_client({})
    client.async_get_snapshot = lambda: _async_return(body)
    coordinator = _make_coordinator(client, contract["snapshot"]["account_id"], contract)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert "does not match" in str(err.value)


async def test_update_auth_error_raises_config_entry_auth_failed(contract) -> None:
    from custom_components.xcreas.api import XcreasAuthError

    async def boom():
        raise XcreasAuthError("invalid_token", 401)

    client = make_client({})
    client.async_get_snapshot = boom
    coordinator = _make_coordinator(client, "acc", contract)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_announces_device_listeners(contract) -> None:
    """§4: a successful inventory announces the new device set to the
    platforms' dynamic add/remove listeners."""
    body = deepcopy(contract["snapshot"])
    client = make_client({})
    client.async_get_snapshot = lambda: _async_return(body)
    coordinator = _make_coordinator(client, body["account_id"], contract)
    seen: list[set] = []
    coordinator.async_add_device_listener(lambda snap: seen.append(set(snap.devices)))
    snapshot = await coordinator._async_update_data()
    assert seen == [set(snapshot.devices)]
    assert coordinator._full_success_seen is True


class _TaskCreator:
    """The minimal hass surface the coordinator's hooks need in tests."""

    def __init__(self) -> None:
        self.created: list[object] = []
        self.flows: list[dict] = []

    def async_create_background_task(self, hass, coro, name):
        self.created.append(coro)
        return asyncio.get_event_loop().create_task(coro)


async def test_changed_notice_debounces_to_one_refresh(contract) -> None:
    """§7: a burst of changed notices coalesces into ONE snapshot pull."""
    from unittest.mock import AsyncMock, MagicMock

    async def fake_get_snapshot():
        return contract["snapshot"]

    client = make_client({})
    client.async_get_snapshot = fake_get_snapshot

    coordinator = XcreasCoordinator.__new__(XcreasCoordinator)
    coordinator._client = client
    coordinator.account_id = contract["snapshot"]["account_id"]
    coordinator._push_refresh_task = None
    coordinator._push_refresh_queued = False
    coordinator.hass = MagicMock()
    # NOTE: this mock covers the COALESCING LOGIC only. The API SURFACE (which
    # method actually schedules the task) is guarded by the real-object test
    # in test_setup_entry.py — MagicMock accepts invented attributes, which is
    # exactly how two invented-API bugs slipped past this file.
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = _TaskCreator().async_create_background_task
    coordinator._async_refresh = AsyncMock()

    # BOTH push entry points schedule through the same coalescing path —
    # async_connection_up must NEVER call a nonexistent scheduler again
    # (the live 'schedule_refresh' AttributeError).
    coordinator.async_connection_up()
    coordinator.async_connection_changed()
    coordinator.async_connection_changed()
    task = coordinator._push_refresh_task
    assert task is not None
    await task
    assert coordinator._async_refresh.await_count == 1


async def test_notice_during_refresh_schedules_trailing_pull(contract) -> None:
    """§3.2: 拉取期间收到新通知时合并为一次后续刷新，不能丢掉最后一次变更 —
    a notice arriving while the pull is in flight must produce a trailing
    refresh (the last change is never swallowed)."""
    from unittest.mock import AsyncMock, MagicMock

    client = make_client({})
    client.async_get_snapshot = lambda: _async_return(contract["snapshot"])

    coordinator = XcreasCoordinator.__new__(XcreasCoordinator)
    coordinator._client = client
    coordinator.account_id = contract["snapshot"]["account_id"]
    coordinator._push_refresh_task = None
    coordinator._push_refresh_queued = False
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.async_create_background_task = _TaskCreator().async_create_background_task

    refreshes = asyncio.Event()
    calls = 0

    async def slow_refresh(**_kwargs):
        # Simulate exactly ONE notice arriving DURING the first pull.
        nonlocal calls
        calls += 1
        if calls == 1:
            coordinator._push_refresh_queued = True
        refreshes.set()
        await asyncio.sleep(0)

    coordinator._async_refresh = slow_refresh
    coordinator.async_connection_changed()
    await coordinator._push_refresh_task
    # Pull 1 observes the queued flag → pull 2 runs and (no new notice)
    # terminates: the LAST change is honored, nothing loops forever.
    assert calls == 2


async def test_revoked_frame_triggers_reauth_flow(contract) -> None:
    """§7: the revoked frame (or auth close) must end the entry."""
    from unittest.mock import MagicMock

    hass = MagicMock()
    flows = []

    def fake_flow_init(domain, context=None, data=None):
        flows.append((domain, context["source"]))
        return None

    hass.config_entries.flow.async_init = fake_flow_init

    coordinator = XcreasCoordinator.__new__(XcreasCoordinator)
    coordinator.hass = hass
    entry = MagicMock()
    entry.domain = "xcreas"
    entry.entry_id = "entry-1"
    entry.data = {}
    coordinator.config_entry = entry

    coordinator.async_connection_revoked()
    assert flows == [("xcreas", "reauth")]


def test_ws_revoked_exception_type() -> None:
    """The revoked marker is distinct from transport errors."""
    from custom_components.xcreas.websocket import XcreasRevoked

    assert issubclass(XcreasRevoked, Exception)


async def _async_return(value):
    return value
