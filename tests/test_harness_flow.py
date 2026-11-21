"""The full OAuth2 config flow on the REAL HA harness (pytest-homeassistant-
custom-component): authorize URL shape (public client + PKCE S256, ACCOUNT
scope), the callback → token exchange → strict snapshot validation → entry
creation, zero-device connect, same-account refresh, cross-account
coexistence, and the re-auth flow (same-account update vs account_mismatch).
This is the T00 "可复现的配置流程试验" for the account-sync contract.
HA target: 2026.10.4 (requirements_test.txt)."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xcreas.const import (
    CONF_ACCOUNT_ID,
    CONF_BASE_URL,
    DOMAIN,
    OAUTH2_CLIENT_ID,
    SNAPSHOT_PATH,
)

from .conftest import CONTRACT

CLIENT_ID = OAUTH2_CLIENT_ID
AUTHORIZE_URL = "https://api.xcreas.com/oauth/ha/authorize"
TOKEN_URL = "https://api.xcreas.com/oauth/ha/token"
REDIRECT_URI = "https://example.com/auth/external/callback"
SNAPSHOT_URL = f"https://api.xcreas.com{SNAPSHOT_PATH}"
ACCOUNT_SCOPE = "homeassistant:account"


@pytest.fixture
async def http_client(hass: HomeAssistant, hass_client_no_auth):
    """An HTTP test client over the http component (hass.http is only set
    once the http component is set up — the disable_http_server autouse
    fixture keeps it socket-free)."""
    assert await async_setup_component(hass, "http", {})
    # The auth component registers /auth/external/callback.
    assert await async_setup_component(hass, "auth", {})
    return await hass_client_no_auth()


def _token_response(access: str) -> dict:
    return {
        "access_token": access,
        "refresh_token": access.replace("ACCESS", "REFRESH"),
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": ACCOUNT_SCOPE,
    }


async def _walk_to_callback(hass, http_client) -> str:
    """Start a user flow, walk the external step and land the callback.
    Returns the flow_id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    resp = await http_client.get(f"/auth/external/callback?code=AAAAAA&state={state}")
    assert resp.status == 200
    return result["flow_id"]


async def test_first_add_from_ui_path_registers_at_flow_time(
    hass, current_request_with_host
) -> None:
    """The EXACT live-failure regression: adding from the UI when the
    component was never loaded runs _load_integration (imports config_flow
    only — NO async_setup). The flow must still reach the external
    authorization step because the pick-implementation step registers the
    implementation at flow time."""
    from homeassistant.helpers import config_entry_oauth2_flow

    assert DOMAIN not in hass.config.components  # component NOT loaded
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    impls = await config_entry_oauth2_flow.async_get_implementations(hass, DOMAIN)
    assert DOMAIN in impls


async def test_component_setup_registers_implementation(hass) -> None:
    """The regression guard for the live missing-configuration abort: the
    component's async_setup alone must register the OAuth2 implementation
    (HA loads the component before starting a UI flow / reauth)."""
    from homeassistant.helpers import config_entry_oauth2_flow

    assert await async_setup_component(hass, DOMAIN, {})
    impls = await config_entry_oauth2_flow.async_get_implementations(hass, DOMAIN)
    assert DOMAIN in impls


async def test_full_flow_creates_entry(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """Login → account consent → callback → token → snapshot → entry."""
    # The REAL UI path: component setup (async_setup registers the
    # implementation) precedes the flow; a request context auto-picks the
    # single implementation and lands on the external authorization step.
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(AUTHORIZE_URL)

    # Public client + PKCE S256 + the ACCOUNT scope on the authorize URL (§2).
    q = parse_qs(urlparse(result["url"]).query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"]
    assert q["scope"] == [ACCOUNT_SCOPE]
    assert q["redirect_uri"] == [REDIRECT_URI]

    state = parse_qs(urlparse(result["url"]).query)["state"][0]

    # The user approves on the xCREAS page; HA receives the callback.
    client = http_client
    resp = await client.get(f"/auth/external/callback?code=AAAAAA&state={state}")
    assert resp.status == 200
    assert "My link" not in await resp.text()

    # The token endpoint (public client: client_id only, no secret) + the
    # snapshot the create step validates.
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-1"))
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])

    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_ID] == CONTRACT["snapshot"]["account_id"]
    assert CONF_BASE_URL in result["data"]
    assert result["data"]["token"]["access_token"] == "ACCESS-1"
    # The account sync stores NO device list (§4: the runtime inventory is
    # the only device source).
    assert not any("device" in key for key in result["data"])

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    # PKCE: the token request must carry the verifier and NO secret.
    token_req = next(r for r in aioclient_mock.mock_calls if r[0] == "POST" and str(r[1]).endswith("/oauth/ha/token"))
    form = token_req[2]  # the mocker records the form fields as kwargs
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == CLIENT_ID
    assert "client_secret" not in form
    assert len(form["code_verifier"]) >= 43


async def test_zero_device_account_connects(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§4: a COMPLETE empty inventory is valid — zero-device accounts
    complete the setup and sync devices as they appear."""
    assert await async_setup_component(hass, DOMAIN, {})
    flow_id = await _walk_to_callback(hass, http_client)
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-2"))
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot_empty"])
    result = await hass.config_entries.flow.async_configure(flow_id)
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_ID] == CONTRACT["snapshot"]["account_id"]


async def test_shared_device_coexists_across_accounts(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§3/§6: a device shared into two accounts does NOT block the second
    account's entry — dedup happens at runtime per stable device identity,
    not at config time."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-existing",
        data={
            CONF_ACCOUNT_ID: "account-existing",
            CONF_BASE_URL: "https://api.xcreas.com",
        },
    )
    existing.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    flow_id = await _walk_to_callback(hass, http_client)
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-3"))
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])
    result = await hass.config_entries.flow.async_configure(flow_id)
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_same_account_refreshes_existing_entry(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§4: authorizing the SAME account again updates the original entry
    (new token) instead of duplicating it."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTRACT["snapshot"]["account_id"],
        data={
            CONF_ACCOUNT_ID: CONTRACT["snapshot"]["account_id"],
            CONF_BASE_URL: "https://api.xcreas.com",
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "OLD",
                "refresh_token": "OLD-REFRESH",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
                "token_type": "Bearer",
                "scope": ACCOUNT_SCOPE,
            },
        },
    )
    existing.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    flow_id = await _walk_to_callback(hass, http_client)
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-NEW"))
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])
    result = await hass.config_entries.flow.async_configure(flow_id)
    await hass.async_block_till_done()
    # HA aborts the redundant flow with already_configured after applying
    # the updates to the existing entry.
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data["token"]["access_token"] == "ACCESS-NEW"


async def test_snapshot_read_failure_surfaces_machine_code(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§2.1: 读取失败 → errors[<machine code>]（可重试反馈）."""
    assert await async_setup_component(hass, DOMAIN, {})
    flow_id = await _walk_to_callback(hass, http_client)
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-4"))
    aioclient_mock.get(
        SNAPSHOT_URL,
        status=503,
        json={"code": 503, "message": "service_unavailable"},
    )
    result = await hass.config_entries.flow.async_configure(flow_id)
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.FORM
    assert result.get("errors") == {"base": "service_unavailable"}


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )


async def test_reauth_updates_same_account_entry(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§4: 常规令牌失效的重新认证校验同一账号并更新原条目 — entities and
    identity survive."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTRACT["snapshot"]["account_id"],
        data={
            CONF_ACCOUNT_ID: CONTRACT["snapshot"]["account_id"],
            CONF_BASE_URL: "https://api.xcreas.com",
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "OLD",
                "refresh_token": "OLD-REFRESH",
                "expires_in": 3600,
                "expires_at": time.time() - 10,  # expired → refresh needed
                "token_type": "Bearer",
                "scope": ACCOUNT_SCOPE,
            },
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    result = await _start_reauth(hass, entry)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    resp = await http_client.get(f"/auth/external/callback?code=BBBBBB&state={state}")
    assert resp.status == 200
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-REAUTH"))
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data["token"]["access_token"] == "ACCESS-REAUTH"


async def test_reauth_rejects_different_account(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§4: 误登另一个账号 → abort account_mismatch，绝不替换原条目."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTRACT["snapshot"]["account_id"],
        data={
            CONF_ACCOUNT_ID: CONTRACT["snapshot"]["account_id"],
            CONF_BASE_URL: "https://api.xcreas.com",
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "OLD",
                "refresh_token": "OLD-REFRESH",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
                "token_type": "Bearer",
                "scope": ACCOUNT_SCOPE,
            },
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    result = await _start_reauth(hass, entry)
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    await http_client.get(f"/auth/external/callback?code=CCCCCC&state={state}")
    aioclient_mock.post(TOKEN_URL, json=_token_response("ACCESS-OTHER"))
    other = {"account_id": "another-account", "inventory_complete": True, "devices": []}
    aioclient_mock.get(SNAPSHOT_URL, json=other)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"
    # The original entry keeps its OLD token.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert entries[0].data["token"]["access_token"] == "OLD"


_ = ConfigEntryState, config_entry_oauth2_flow
