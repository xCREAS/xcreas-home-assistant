"""Config flow for the xCREAS integration.

The flow rides HA's standard OAuth2 config-entry flow (authorization code +
PKCE S256 against the xCREAS public client) and extends the create step with
the account/device checks the product requires (docs/ha-integration.md §2/§3):

- one config entry per xCREAS account (unique_id = account_id);
- an authorized device may only be controlled by ONE entry per HA instance
  (`device_already_linked` abort — 归属 = 先连者持有；撤销后另一账号可重新添加);
- a completely empty selection aborts (`no_devices_authorized`).
"""

from __future__ import annotations

import logging
from typing import Any

from typing import override
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow

from .api import XcreasApiClient, XcreasApiError, parse_snapshot
from .const import (
    CONF_ACCOUNT_ID,
    CONF_BASE_URL,
    CONF_LINKED_DEVICE_IDS,
    DEFAULT_BASE_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class XcreasFlowError(HomeAssistantError):
    """Snapshot validation failed with a machine code."""


class XcreasFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle the xCREAS config flow (OAuth2 + account/device checks)."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @override
    async def async_step_pick_implementation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Register the implementation AT FLOW TIME, then pick.

        The first add from the UI imports this module via
        _load_integration WITHOUT running async_setup (verified against HA
        2026.10.4 config_entries.py: the user-flow path only imports the
        config_flow platform), so an async_setup-only registration never
        exists when the pick step looks it up — the live
        "missing_configuration" abort. Registering here is idempotent and
        covers EVERY entry into the flow (user add, reauth); the async_setup
        registration in __init__.py stays for the entry-runtime token
        resolution path (entry setup → component setup).
        """
        from .oauth2 import async_register_xcreas_implementation

        await async_register_xcreas_implementation(self.hass, DEFAULT_BASE_URL)
        return await super().async_step_pick_implementation(user_input)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Validate the account snapshot before creating the entry."""
        base_url = DEFAULT_BASE_URL
        token = (data.get("token") or {}).get("access_token", "")
        try:
            snapshot = await _validated_snapshot(self.hass, base_url, token)
        except XcreasFlowError as err:
            # Retryable feedback on the auth step (任务规格 §3: 回调出错给出
            # 可重试反馈) — re-doing the external step re-runs the OAuth dance.
            return self.async_show_form(step_id="auth", errors={"base": str(err)})
        account_id = snapshot.get("account_id")
        devices = parse_snapshot(snapshot)
        if not account_id:
            return self.async_abort(reason="account_unresolved")
        if not devices:
            # 零选中：不产生有效连接，用户重新开始授权并在 xCREAS 页面勾选。
            return self.async_abort(reason="no_devices_authorized")
        device_ids = [d["device_id"] for d in devices]

        # 归属检查：同一台设备在本 HA 实例内只属于一个 xCREAS 条目。
        overlap = sorted(set(device_ids) & _linked_devices(self.hass, exclude_account=account_id))
        if overlap:
            _LOGGER.info("devices already linked to another xcreas entry: %s", overlap)
            return self.async_abort(reason="device_already_linked")

        await self.async_set_unique_id(account_id)
        self._abort_if_unique_id_configured()

        entry_data = dict(data)
        entry_data[CONF_ACCOUNT_ID] = account_id
        entry_data[CONF_LINKED_DEVICE_IDS] = device_ids
        entry_data.setdefault(CONF_BASE_URL, base_url)
        return self.async_create_entry(title="xCREAS", data=entry_data)


async def _validated_snapshot(
    hass, base_url: str, access_token: str
) -> dict[str, Any]:
    """Fetch the snapshot with the flow's fresh token (pre-entry)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = XcreasApiClient(
        async_get_clientsession(hass), base_url, _static_token(access_token)
    )
    try:
        return await client.async_get_snapshot()
    except XcreasApiError as err:
        _LOGGER.warning("snapshot validation failed: %s", err)
        raise XcreasFlowError(err.code) from err


def _static_token(access_token: str):
    async def _get() -> str:
        return access_token

    return _get


def _linked_devices(hass, *, exclude_account: str | None) -> set[str]:
    """Device ids authorized by OTHER xcreas entries of this instance."""
    linked: set[str] = set()
    for entry in hass.config_entries.async_entries():
        if entry.domain != DOMAIN:
            continue
        if exclude_account is not None and entry.data.get(CONF_ACCOUNT_ID) == exclude_account:
            continue
        stored = entry.data.get(CONF_LINKED_DEVICE_IDS)
        if isinstance(stored, list):
            linked.update(stored)
    return linked
