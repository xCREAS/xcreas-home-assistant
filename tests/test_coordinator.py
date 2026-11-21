"""Coordinator tests: snapshot parsing, merge-pull debounce, revoked →
reauth, and the entity-cleanup-only-on-full-success rule (docs/
ha-integration.md §3/§7)."""

from __future__ import annotations

import asyncio

from custom_components.xcreas.coordinator import XcreasCoordinator, XcreasSnapshot

from .conftest import make_client


def test_snapshot_parse(contract) -> None:
    client = make_client({})
    body = contract["snapshot"]
    snapshot = XcreasSnapshot(account_id=body["account_id"])
    from custom_components.xcreas.api import parse_snapshot

    for raw in parse_snapshot(body):
        snapshot.devices[raw["device_id"]] = None
    _ = client
    assert list(snapshot.devices) == ["V350s-001"]


def test_parse_snapshot_empty_and_malformed(contract) -> None:
    from custom_components.xcreas.api import parse_snapshot

    assert parse_snapshot({}) == []
    assert parse_snapshot({"devices": None}) == []
    assert parse_snapshot({"devices": ["not-a-dict", contract["snapshot"]["devices"][0]]}) == [
        contract["snapshot"]["devices"][0]
    ]


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

    pulls = 0

    async def fake_get_snapshot():
        nonlocal pulls
        pulls += 1
        return contract["snapshot"]

    client = make_client({})
    client.async_get_snapshot = fake_get_snapshot

    coordinator = XcreasCoordinator.__new__(XcreasCoordinator)
    coordinator._client = client
    coordinator.account_id = contract["snapshot"]["account_id"]
    coordinator._push_refresh_task = None
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
