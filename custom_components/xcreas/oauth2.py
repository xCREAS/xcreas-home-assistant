"""OAuth2 implementation for the xCREAS public client (PKCE S256).

docs/ha-integration.md §2: the cloud accepts ONLY the public-client form —
client_id-only token auth with a mandatory S256 code challenge. HA 2026.11's
`LocalOAuth2ImplementationWithPkce` provides exactly that shape (empty
client_secret, 128-char verifier).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow


class XcreasOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    """The xCREAS public client (no secret, PKCE S256 mandated server-side)."""

    def __init__(
        self,
        hass: HomeAssistant,
        domain: str,
        client_id: str,
        authorize_url: str,
        token_url: str,
        scopes: list[str],
    ) -> None:
        # LocalOAuth2ImplementationWithPkce(hass, domain, client_id,
        # authorize_url, token_url, client_secret="", ...) — the PKCE
        # subclass orders client_secret AFTER the urls.
        super().__init__(
            hass,
            domain,
            client_id,
            authorize_url,
            token_url,
        )
        self._scopes = scopes

    @property
    def name(self) -> str:
        return "xCREAS"

    @property
    def extra_authorize_data(self) -> dict:
        data = super().extra_authorize_data
        data["scope"] = " ".join(self._scopes)
        return data


async def async_register_xcreas_implementation(hass: HomeAssistant, base_url: str) -> None:
    """Register (or replace) the implementation for this domain.

    base_url is configurable so a test deployment can point the flow at a
    staging API; production leaves the default.
    """
    from .const import (
        AUTHORIZE_URL,
        DOMAIN,
        OAUTH2_CLIENT_ID,
        SCOPES,
        TOKEN_URL,
    )

    authorize = AUTHORIZE_URL
    token = TOKEN_URL
    if base_url and base_url.rstrip("/") != "https://api.xcreas.com":
        authorize = f"{base_url.rstrip('/')}/oauth/ha/authorize"
        token = f"{base_url.rstrip('/')}/oauth/ha/token"

    implementation = XcreasOAuth2Implementation(
        hass,
        DOMAIN,
        OAUTH2_CLIENT_ID,
        authorize,
        token,
        list(SCOPES),
    )
    # Registration is idempotent per domain (the helper keeps a registry);
    # replacing a prior registration keeps re-auth flows pointing at the
    # same URLs.
    existing = hass.data.get(config_entry_oauth2_flow.DATA_IMPLEMENTATIONS, {}).get(DOMAIN)
    if existing:
        # Existing registration: leave it — the URLs are deployment-stable and
        # the config flow aborts on a differing base URL anyway.
        return
    config_entry_oauth2_flow.async_register_implementation(hass, DOMAIN, implementation)
