"""Config-flow unit checks: the account/device gates (docs/ha-integration.md
§2/§3). The full OAuth2 browser dance is covered by the HA harness flow
helpers where practical; these tests pin the create-entry decisions."""

from __future__ import annotations

import pytest


def test_linked_devices_overlap_logic(contract) -> None:
    """§3: 同一台设备在本 HA 实例内只属于一个条目 — the overlap set is
    computed against OTHER entries' stored linked_device_ids."""
    from custom_components.xcreas.config_flow import _linked_devices
    from custom_components.xcreas.const import CONF_ACCOUNT_ID, CONF_LINKED_DEVICE_IDS, DOMAIN

    class Entry:
        domain = DOMAIN

        def __init__(self, data):
            self.data = data

    class Entries:
        def __init__(self, entries):
            self._entries = entries

        def async_entries(self):
            return self._entries

    class Hass:
        config_entries = Entries(
            [
                Entry(
                    {
                        CONF_ACCOUNT_ID: "account-a",
                        CONF_LINKED_DEVICE_IDS: ["V350s-001"],
                    }
                ),
                Entry(
                    {
                        CONF_ACCOUNT_ID: "account-b",
                        CONF_LINKED_DEVICE_IDS: ["V350s-002"],
                    }
                ),
            ]
        )

    devices = {d["device_id"] for d in contract["snapshot"]["devices"]}
    # account-a re-authorizing: its own devices are excluded from the check.
    assert _linked_devices(Hass(), exclude_account="account-a") == {"V350s-002"}
    # a NEW account (account-c): every already-linked device overlaps.
    assert _linked_devices(Hass(), exclude_account="account-c") == {"V350s-001", "V350s-002"}
    _ = devices, DOMAIN


def test_scope_constant_matches_contract(contract) -> None:
    """The HA client requests exactly the documented scope."""
    from custom_components.xcreas.const import SCOPES

    assert SCOPES == ["homeassistant"]
