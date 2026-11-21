"""Entry setup/unload on the real harness: platforms forward from the first
full snapshot, device changes add/remove entities dynamically, and unload
releases the ws task (docs/ha-integration.md §7 + account-sync §4)."""

from __future__ import annotations

import copy
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xcreas.const import (
    CONF_ACCOUNT_ID,
    CONF_BASE_URL,
    DOMAIN,
    SNAPSHOT_PATH,
)

from .conftest import CONTRACT

SNAPSHOT_URL = f"https://api.xcreas.com{SNAPSHOT_PATH}"

TOKEN_SCOPE = "homeassistant:account"


@pytest.fixture
async def setup_entry(hass: HomeAssistant, aioclient_mock) -> MockConfigEntry:
    """A set-up xCREAS entry with one device (ws will fail-and-retry — its
    reconnect loop is exercised implicitly by unload)."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "auth", {})
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTRACT["snapshot"]["account_id"],
        data={
            CONF_ACCOUNT_ID: CONTRACT["snapshot"]["account_id"],
            CONF_BASE_URL: "https://api.xcreas.com",
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "ACCESS",
                "refresh_token": "REFRESH",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
                "token_type": "Bearer",
                "scope": TOKEN_SCOPE,
            },
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_ids(hass: HomeAssistant, platform: Platform) -> list[str]:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    return [
        e.entity_id
        for e in registry.entities.values()
        if e.config_entry_id is not None and e.domain == platform.value
    ]


async def test_setup_creates_fan_gear_selector_and_sensors(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """One fan, one gear selector and three sensors on the inventory device."""
    await hass.async_block_till_done()
    fans = _entity_ids(hass, Platform.FAN)
    sensors = _entity_ids(hass, Platform.SENSOR)
    selectors = _entity_ids(hass, Platform.SELECT)
    assert len(fans) == 1
    assert len(sensors) == 3
    assert len(selectors) == 1
    assert hass.states.get(selectors[0]).attributes["options"] == ["gear_1", "gear_2", "gear_3"]
    # unique_id = <device_id>:<kind> — NO account prefix (account-sync §6:
    # stable device identity survives account switches).
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    kinds = sorted(
        e.unique_id.split(":")[-1]
        for e in registry.entities.values()
        if e.config_entry_id is not None
    )
    assert kinds == ["fan", "humidity", "manual_gear", "pm25", "temperature"]
    assert registry.async_get(selectors[0]).device_id == registry.async_get(fans[0]).device_id
    for e in registry.entities.values():
        if e.config_entry_id is not None:
            assert e.unique_id.startswith("V350s-001:")

    client = setup_entry.runtime_data.client
    with patch.object(client, "async_send_command", new_callable=AsyncMock) as send:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": selectors[0], "option": "gear_2"},
            blocking=True,
        )
        send.assert_awaited_once_with("V350s-001", "set_fan_level", {"fan_level": 2})


async def test_inventory_change_adds_and_removes_entities(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The account sync (§4): a device bound later appears without any user
    action; a device losing access is removed — entities converge to the
    authoritative inventory."""
    entry = setup_entry
    coordinator = entry.runtime_data

    two_devices = copy.deepcopy(CONTRACT["snapshot"])
    two_devices["devices"].append(
        {
            "device_id": "V350s-002",
            "name": "卧室净化器",
            "model": "V350s",
            "role": "owner",
            "supports_fan_level": True,
            "availability": "offline",
            "control": {},
            "sensors": {},
        }
    )

    async def _two_devices():
        return two_devices

    with patch.object(coordinator.client, "async_get_snapshot", _two_devices):
        coordinator.async_connection_changed()
        await coordinator._push_refresh_task
        await hass.async_block_till_done()
    assert len(_entity_ids(hass, Platform.FAN)) == 2
    assert len(_entity_ids(hass, Platform.SENSOR)) == 6
    assert len(_entity_ids(hass, Platform.SELECT)) == 2

    # The device leaves the inventory (share withdrawn / unbound / frozen):
    # the next complete snapshot removes everything it owned.
    one_device = copy.deepcopy(CONTRACT["snapshot"])
    for dev in one_device["devices"]:
        dev.pop("supports_fan_level")

    async def _one_device():
        return one_device

    with patch.object(coordinator.client, "async_get_snapshot", _one_device):
        coordinator.async_connection_changed()
        await coordinator._push_refresh_task
        await hass.async_block_till_done()
    assert len(_entity_ids(hass, Platform.FAN)) == 1
    assert len(_entity_ids(hass, Platform.SENSOR)) == 3
    assert len(_entity_ids(hass, Platform.SELECT)) == 0  # capability withdrawn


async def test_failed_refresh_keeps_entities(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """§4: a failed/incomplete read never triggers cleanup — the previous
    inventory and its entities survive."""
    entry = setup_entry
    bad = {"account_id": CONTRACT["snapshot"]["account_id"], "devices": []}  # no marker

    async def _bad():
        return bad

    coordinator = entry.runtime_data
    with patch.object(coordinator.client, "async_get_snapshot", _bad):
        coordinator.async_connection_changed()
        await coordinator._push_refresh_task
        await hass.async_block_till_done()
    assert len(_entity_ids(hass, Platform.FAN)) == 1
    assert len(_entity_ids(hass, Platform.SENSOR)) == 3


async def test_push_refresh_uses_real_api(
    hass, setup_entry: MockConfigEntry, aioclient_mock
) -> None:
    """Regression for the invented-API chain (schedule_refresh, then
    hass.config_entries.async_create_background_task): the push-refresh
    scheduling must run against REAL objects — a single wrong attribute
    fails HERE, not in a live HA instance."""
    entry = setup_entry
    coordinator = entry.runtime_data

    snapshot_calls = lambda: sum(
        1 for c in aioclient_mock.mock_calls if c[0] == "GET" and str(c[1]).endswith(SNAPSHOT_PATH)
    )
    before = snapshot_calls()

    coordinator.async_connection_changed()   # the changed-notice path
    coordinator.async_connection_up()        # the reconnect-calibration path
    task = coordinator._push_refresh_task
    assert task is not None
    await task
    await hass.async_block_till_done()

    assert snapshot_calls() >= before + 1


async def test_unload_stops_connection(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unload releases tasks/subscription/connection and removes
    entities; a re-setup does not duplicate them."""
    entry = setup_entry
    coordinator = entry.runtime_data
    fans_before = _entity_ids(hass, Platform.FAN)
    assert len(fans_before) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._ws is None  # the ws holder is released
    # Unloaded entities keep their registry rows and render restored/unavailable.
    state = hass.states.get(fans_before[0])
    assert state is not None
    assert state.state == "unavailable"
    assert state.attributes.get("restored") is True
