"""Contract tests for the REST client (docs/ha-integration.md §4/§5).

These assertions read the SAME sample shapes the API-side apitest asserts
(tests/fixtures/contract_samples.json ↔ xcreas-api tests/apitest/
ha_integration_test.go).
"""

from __future__ import annotations

import pytest

from custom_components.xcreas.api import (
    XcreasApiError,
    XcreasAuthError,
    parse_snapshot,
)
from custom_components.xcreas.const import COMMAND_PATH, SNAPSHOT_PATH

from .conftest import _HttpError, make_client


async def test_snapshot_request_shape(contract) -> None:
    client = make_client({("GET", SNAPSHOT_PATH): contract["snapshot"]})
    body = await client.async_get_snapshot()
    assert body == contract["snapshot"]
    devices = parse_snapshot(body)
    assert [d["device_id"] for d in devices] == ["V350s-001"]


async def test_snapshot_converts_humidity_once(contract) -> None:
    """§4: humidity is humidity_x100/100, converted ONCE on the API side —
    the HA side must read the converted value verbatim (47.5, not 4750)."""
    client = make_client({("GET", SNAPSHOT_PATH): contract["snapshot"]})
    body = await client.async_get_snapshot()
    humidity = body["devices"][0]["sensors"]["humidity"]
    assert humidity["value"] == 47.5


async def test_command_request_and_response(contract) -> None:
    client = make_client({("POST", COMMAND_PATH): contract["command_response_applied"]})
    resp = await client.async_send_command(
        contract["command_request"]["device_id"],
        contract["command_request"]["action"],
        contract["command_request"]["params"],
        contract["command_request"]["request_id"],
    )
    assert resp["status"] == "applied"
    assert resp["command_id"]


async def test_command_machine_error_mapping(contract) -> None:
    """The envelope {"code": <status>, "message": "<machine code>"} maps to
    typed errors carrying the machine code (§5 error table)."""
    for fixture, status, expected_code in [
        ("command_error_offline", 409, "device_offline"),
        ("command_error_conflict", 409, "request_id_conflict"),
        ("command_error_capability", 422, "capability_not_supported"),
    ]:
        client = make_client({("POST", COMMAND_PATH): _HttpError(status, contract[fixture])})
        with pytest.raises(XcreasApiError) as err:
            await client.async_send_command("V350s-001", "set_fan_speed", {"percentage": 50})
        assert err.value.code == expected_code
        assert err.value.status == status


async def test_snapshot_retryable_errors_are_not_auth(contract) -> None:
    """§4: the inventory budget / access-read failures are retryable 503s —
    NOT auth errors (the entry must keep its previous device list, never
    start a re-auth)."""
    for fixture in ("command_error_snapshot_too_large",):
        client = make_client({("GET", SNAPSHOT_PATH): _HttpError(503, contract[fixture])})
        with pytest.raises(XcreasApiError) as err:
            await client.async_get_snapshot()
        assert not isinstance(err.value, XcreasAuthError)
        assert err.value.code == "snapshot_too_large"


async def test_auth_error_is_typed(contract) -> None:
    client = make_client(
        {("GET", SNAPSHOT_PATH): _HttpError(401, {"code": 401, "message": "invalid_token"})}
    )
    with pytest.raises(XcreasAuthError):
        await client.async_get_snapshot()


async def test_request_id_charset(contract) -> None:
    """The generated idempotency id matches the server charset (8..128
    URL-safe runes; §5)."""
    client = make_client({})
    rid = client.new_request_id()
    assert 8 <= len(rid) <= 128
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    assert set(rid) <= allowed
