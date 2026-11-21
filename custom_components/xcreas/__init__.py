"""The xCREAS Home Assistant integration (account-level device sync).

One config entry = one xCREAS account (one OAuth grant, scope
`homeassistant:account`). Setup builds the API client + coordinator, starts
the /ws/ha push connection and forwards the fan, select and sensor platforms;
the DEVICE SET is maintained automatically from the authoritative snapshot —
bound/shared devices appear, unbound/withdrawn/frozen devices are removed,
with no re-authorization (docs/ha-integration.md §3/§7). Unload releases the
connection and its task.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .api import XcreasApiClient
from .const import CONF_ACCOUNT_ID, CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN, WS_PATH
from .coordinator import XcreasCoordinator
from .websocket import XcreasWebsocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.FAN, Platform.SELECT, Platform.SENSOR]

# Config-entry-only integration: the YAML config slot must stay empty
# (hassfest requires a schema from integrations defining async_setup).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Component setup — registers the OAuth2 implementation for the domain.

    This is THE registration point (HA's canonical pattern for OAuth2-helper
    integrations): HA loads the component BEFORE starting a UI config flow
    and before any reauth, so the implementation is always present when the
    flow's pick-implementation step runs. Registering inside a flow step
    does NOT cover the real UI path — the missing-configuration abort seen
    in the first live test is exactly that mistake.
    """
    from .oauth2 import async_register_xcreas_implementation

    hass.data.setdefault(DOMAIN, {})
    await async_register_xcreas_implementation(hass, DEFAULT_BASE_URL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one xCREAS account entry."""
    base_url = entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
    account_id = entry.data.get(CONF_ACCOUNT_ID, "")
    implementation = await _get_implementation(hass)
    session = _oauth2_session(hass, entry, implementation)

    async def _access_token() -> str:
        await session.async_ensure_token_valid()
        return session.token["access_token"]

    client = XcreasApiClient(_websession(hass), base_url, _access_token)
    coordinator = XcreasCoordinator(hass, client, account_id, config_entry=entry)

    # Fetch the first snapshot BEFORE forwarding platforms so the device
    # registry entries (and their names) exist when entities attach.
    await coordinator.async_config_entry_first_refresh()

    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + WS_PATH
    ws = XcreasWebsocket(coordinator, _websession(hass), ws_url, _access_token)
    coordinator.attach_ws(ws)
    await ws.start()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Startup sweep (stale-devices rule): the full snapshot is authoritative
    # — remove THIS entry's device-registry rows for devices no longer in the
    # inventory (the entity reconcile removes their entities; the delayed
    # sweep clears the registry rows this integration left behind).
    entry.async_create_background_task(
        hass, _cleanup_stale_registry(hass, entry, coordinator), name="xcreas-startup-cleanup"
    )
    return True


async def _cleanup_stale_registry(hass: HomeAssistant, entry: ConfigEntry, coordinator: XcreasCoordinator) -> None:
    """Delayed device-registry sweep against the startup inventory."""
    from homeassistant.helpers import device_registry as dr

    await asyncio.sleep(10)  # let the platform reconciles finish removing entities
    registry = dr.async_get(hass)
    valid = set(coordinator.data.devices) if coordinator.data else set()
    for registry_entry in list(registry.devices.values()):
        for domain, value in registry_entry.identifiers:
            if domain != DOMAIN or value in valid:
                continue
            if entry.entry_id not in registry_entry.config_entries:
                continue
            registry.async_remove_device(registry_entry.id)
            _LOGGER.info("xcreas: removed stale device registry entry %s", value)
            break


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload: release tasks, subscription and connection (spec T02)."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: XcreasCoordinator = entry.runtime_data
        await coordinator.shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options/base-URL changes reload the entry (HA's standard semantics)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _websession(hass: HomeAssistant):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return async_get_clientsession(hass)


def _oauth2_session(hass: HomeAssistant, entry: ConfigEntry, implementation):
    from homeassistant.helpers import config_entry_oauth2_flow

    return config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)


async def _get_implementation(hass: HomeAssistant):
    from homeassistant.helpers import config_entry_oauth2_flow

    # Registered by async_setup (component load precedes entry setup).
    implementations = await config_entry_oauth2_flow.async_get_implementations(
        hass, DOMAIN
    )
    return implementations[DOMAIN]
