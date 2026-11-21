"""Config-flow unit checks for the account-level gates (docs/ha-integration.md
§2/§4). The full OAuth2 browser dance is covered by the HA harness flow
helpers where practical; these tests pin the flow's constants and the strict
snapshot validation it depends on."""

from __future__ import annotations

import pytest

from custom_components.xcreas.api import XcreasSnapshotInvalid, parse_snapshot


def test_scope_constant_matches_contract(contract) -> None:
    """§2: the HA client requests exactly the documented ACCOUNT scope."""
    from custom_components.xcreas.const import SCOPES

    assert SCOPES == ["homeassistant:account"]


def test_parse_snapshot_accepts_zero_devices(contract) -> None:
    """§4: a COMPLETE empty inventory is valid — zero-device accounts may
    connect; the runtime maintains the device set afterwards."""
    assert parse_snapshot(contract["snapshot_empty"]) == []


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b.pop("inventory_complete"), "missing_inventory_complete"),
        (lambda b: b.__setitem__("inventory_complete", False), "missing_inventory_complete"),
        (lambda b: b.__setitem__("devices", {"V350s-001": {}}), "bad_devices"),
        (lambda b: b["devices"].append({"device_id": "V350s-001", "name": "dup"}), "duplicate_device_id"),
        (lambda b: b["devices"].append({"device_id": ""}), "bad_device_block"),
        (lambda b: b["devices"].append("V350s-009"), "bad_device_block"),
        (lambda b: b.pop("account_id"), "missing_account_id"),
    ],
)
def test_parse_snapshot_strict_validation(contract, mutate, reason) -> None:
    """§4: field missing / wrong type / duplicate device ids invalidate the
    WHOLE inventory — parse never silently drops devices."""
    body = contract["snapshot"]
    mutate(body)
    with pytest.raises(XcreasSnapshotInvalid) as err:
        parse_snapshot(body)
    assert err.value.args[0] == reason
