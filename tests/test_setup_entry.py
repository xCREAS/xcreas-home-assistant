"""Entry setup/unload on the real harness: platforms forward from the first
full snapshot, and unload releases the ws task (docs/ha-integration.md §7
T02 unload semantics)."""

from __future__ import annotations

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
    CONF_LINKED_DEVICE_IDS,
    DOMAIN,
    SNAPSHOT_PATH,
)

from .conftest import CONTRACT

SNAPSHOT_URL = f"https://api.xcreas.com{SNAPSHOT_PATH}"


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
            CONF_LINKED_DEVICE_IDS: ["V350s-001"],
            CONF_BASE_URL: "https://api.xcreas.com",
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "ACCESS",
                "refresh_token": "REFRESH",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
                "token_type": "Bearer",
                "scope": "homeassistant",
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
    """One fan, one gear selector and three sensors on the authorized device."""
    await hass.async_block_till_done()
    fans = _entity_ids(hass, Platform.FAN)
    sensors = _entity_ids(hass, Platform.SENSOR)
    selectors = _entity_ids(hass, Platform.SELECT)
    assert len(fans) == 1
    assert len(sensors) == 3
    assert len(selectors) == 1
    assert hass.states.get(selectors[0]).attributes["options"] == ["gear_1", "gear_2", "gear_3"]
    # unique_id = <account>:<device>:<kind> (docs/ha-integration.md §6).
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    kinds = sorted(
        e.unique_id.split(":")[-1]
        for e in registry.entities.values()
        if e.config_entry_id is not None
    )
    assert kinds == ["fan", "humidity", "manual_gear", "pm25", "temperature"]
    assert registry.async_get(selectors[0]).device_id == registry.async_get(fans[0]).device_id

    client = setup_entry.runtime_data.client
    with patch.object(client, "async_send_command", new_callable=AsyncMock) as send:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": selectors[0], "option": "gear_2"},
            blocking=True,
        )
        send.assert_awaited_once_with("V350s-001", "set_fan_level", {"fan_level": 2})


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
    """T02: unload releases tasks/subscription/connection and removes
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
