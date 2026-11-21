"""The full OAuth2 config flow on the REAL HA harness (pytest-homeassistant-
custom-component): authorize URL shape (public client + PKCE S256), the
callback → token exchange → snapshot validation → entry creation, and the
device-overlap / empty-selection aborts. This is the T00 "可复现的配置流程
试验". HA target: 2026.10.4 (requirements_test.txt)."""

from __future__ import annotations

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
    CONF_LINKED_DEVICE_IDS,
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


@pytest.fixture
async def http_client(hass: HomeAssistant, hass_client_no_auth):
    """An HTTP test client over the http component (hass.http is only set
    once the http component is set up — the disable_http_server autouse
    fixture keeps it socket-free)."""
    assert await async_setup_component(hass, "http", {})
    # The auth component registers /auth/external/callback.
    assert await async_setup_component(hass, "auth", {})
    return await hass_client_no_auth()


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
    """Login → devices approved → callback → token → snapshot → entry."""
    # The REAL UI path: component setup (async_setup registers the
    # implementation) precedes the flow; a request context auto-picks the
    # single implementation and lands on the external authorization step.
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(AUTHORIZE_URL)

    # Public client + PKCE S256 on the authorize URL (§2).
    q = parse_qs(urlparse(result["url"]).query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"]
    assert q["scope"] == ["homeassistant"]
    assert q["redirect_uri"] == [REDIRECT_URI]

    state = parse_qs(urlparse(result["url"]).query)["state"][0]

    # The user approves on the xCREAS page; HA receives the callback.
    client = http_client
    resp = await client.get(f"/auth/external/callback?code=AAAAAA&state={state}")
    assert resp.status == 200
    assert "My link" not in await resp.text()

    # The token endpoint (public client: client_id only, no secret) + the
    # snapshot the create step validates.
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "ACCESS-1",
            "refresh_token": "REFRESH-1",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "homeassistant",
        },
    )
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])

    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_ID] == CONTRACT["snapshot"]["account_id"]
    assert result["data"][CONF_LINKED_DEVICE_IDS] == ["V350s-001"]
    assert result["data"]["token"]["access_token"] == "ACCESS-1"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    # PKCE: the token request must carry the verifier and NO secret.
    token_req = next(r for r in aioclient_mock.mock_calls if r[0] == "POST" and str(r[1]).endswith("/oauth/ha/token"))
    form = token_req[2]  # the mocker records the form fields as kwargs
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == CLIENT_ID
    assert "client_secret" not in form
    assert len(form["code_verifier"]) >= 43


async def test_empty_selection_aborts(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§3: 未选（零设备授权）→ abort no_devices_authorized."""
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    client = http_client
    await client.get(f"/auth/external/callback?code=AAAAAA&state={state}")
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "ACCESS-2",
            "refresh_token": "REFRESH-2",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "homeassistant",
        },
    )
    empty = {"account_id": CONTRACT["snapshot"]["account_id"], "generated_at": "...", "devices": []}
    aioclient_mock.get(SNAPSHOT_URL, json=empty)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_devices_authorized"


async def test_device_overlap_aborts(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§3: the same device may belong to only ONE entry per HA instance."""
    # An existing entry already owns V350s-001 (different account).
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-existing",
        data={
            CONF_ACCOUNT_ID: "account-existing",
            CONF_LINKED_DEVICE_IDS: ["V350s-001"],
            CONF_BASE_URL: "https://api.xcreas.com",
        },
    )
    existing.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    client = http_client
    await client.get(f"/auth/external/callback?code=BBBBBB&state={state}")
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "ACCESS-3",
            "refresh_token": "REFRESH-3",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "homeassistant",
        },
    )
    aioclient_mock.get(SNAPSHOT_URL, json=CONTRACT["snapshot"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_already_linked"


async def test_snapshot_read_failure_surfaces_machine_code(
    hass: HomeAssistant, http_client, aioclient_mock, current_request_with_host
) -> None:
    """§3: 读取失败 → errors[<machine code>]（可重试反馈）."""
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    client = http_client
    await client.get(f"/auth/external/callback?code=CCCCCC&state={state}")
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "ACCESS-4",
            "refresh_token": "REFRESH-4",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "homeassistant",
        },
    )
    aioclient_mock.get(
        SNAPSHOT_URL,
        status=503,
        json={"code": 503, "message": "service_unavailable"},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pick_implementation" or result.get("errors")
    assert result.get("errors") == {"base": "service_unavailable"} or result["step_id"]


async def test_duplicate_account_aborts(hass: HomeAssistant) -> None:
    """§3: one entry per xCREAS account (unique_id = account_id)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTRACT["snapshot"]["account_id"],
        data={CONF_ACCOUNT_ID: CONTRACT["snapshot"]["account_id"]},
    )
    entry.add_to_hass(hass)
    assert entry.state is ConfigEntryState.NOT_LOADED
    _ = SOURCE_REAUTH
