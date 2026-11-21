"""Config flow for the xCREAS integration.

The flow rides HA's standard OAuth2 config-entry flow (authorization code +
PKCE S256 against the xCREAS public client) and validates the ACCOUNT-level
grant before creating/updating the entry (docs/ha-integration.md §2/§3/§4):

- one config entry per xCREAS account (unique_id = account_id); a repeated
  authorization of the same account updates the existing entry;
- ZERO devices is a valid state — the account sync maintains the device set
  automatically, so the flow never blocks on the device count and stores no
  device list;
- the FIRST account snapshot must be strictly complete (account_id,
  inventory_complete:true, unique device ids) — a failed validation is
  retryable feedback on the auth step;
- re-authentication validates the SAME account (a different account
  aborts with `account_mismatch`) and updates the original entry.
"""

from __future__ import annotations

import logging
from typing import Any

from typing import override
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow

from .api import XcreasApiClient, XcreasApiError, XcreasSnapshotInvalid, parse_snapshot
from .const import CONF_ACCOUNT_ID, CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class XcreasFlowError(HomeAssistantError):
    """Snapshot validation failed with a machine code."""


class XcreasFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle the xCREAS config flow (OAuth2 + account checks)."""

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

    async def async_step_reauth(self, entry_data) -> ConfigFlowResult:
        """The grant died (revoked frame / dead refresh token): re-run the
        OAuth2 dance for the SAME entry."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        """Show why the re-auth is needed; continue into the external step."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation(None)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Validate the account snapshot, then create or re-auth the entry."""
        base_url = DEFAULT_BASE_URL
        token = (data.get("token") or {}).get("access_token", "")
        reauth_entry = getattr(self, "_reauth_entry", None)
        try:
            snapshot = await _validated_snapshot(self.hass, base_url, token)
        except XcreasFlowError as err:
            # Retryable feedback on the auth step (任务规格 §2.1: 回调出错给出
            # 可重试反馈) — re-doing the external step re-runs the OAuth dance.
            return self.async_show_form(step_id="auth", errors={"base": str(err)})
        account_id = snapshot.get("account_id")
        if not account_id:
            return self.async_abort(reason="account_unresolved")
        try:
            # Strict completeness validation (§4) — the flow requires the
            # SAME contract the runtime enforces, but zero devices is fine.
            parse_snapshot(snapshot)
        except XcreasSnapshotInvalid as err:
            _LOGGER.warning("snapshot failed strict validation: %s", err.args[0])
            return self.async_show_form(
                step_id="auth", errors={"base": "snapshot_invalid"}
            )

        entry_data = dict(data)
        entry_data[CONF_ACCOUNT_ID] = account_id
        entry_data.setdefault(CONF_BASE_URL, base_url)

        if reauth_entry is not None:
            # 重新认证必须还是同一个账号 (V07): a different account must NOT
            # silently take over the entry — abort with a distinct reason.
            if reauth_entry.data.get(CONF_ACCOUNT_ID) != account_id:
                _LOGGER.info(
                    "reauth account mismatch: entry=%s login=%s",
                    reauth_entry.data.get(CONF_ACCOUNT_ID),
                    account_id,
                )
                return self.async_abort(reason="account_mismatch")
            self.hass.config_entries.async_update_entry(reauth_entry, data=entry_data)
            await self.hass.config_entries.async_reload(reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(account_id)
        # One entry per account (stable account_id): authorizing the SAME
        # account again UPDATES the existing entry (token + data) and reloads
        # it — HA's canonical `_abort_if_unique_id_configured(updates=...)`
        # semantics — while a DIFFERENT account creates its own entry.
        self._abort_if_unique_id_configured(updates=entry_data)
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
