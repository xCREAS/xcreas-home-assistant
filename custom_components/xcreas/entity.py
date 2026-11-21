"""Shared entity helpers (docs/ha-integration.md §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import XcreasApiError
from .const import DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from .coordinator import XcreasCoordinator, XcreasDeviceData


def unique_id_for(device_id: str, kind: str) -> str:
    """The entity registry unique_id (§6): `<device_id>:<kind>` — shared by
    the dedup logic in the platform reconcile loops."""
    return f"{device_id}:{kind}"


def device_registry_id(device_id: str) -> str:
    """The device registry identifier value for a cloud device id."""
    return device_id


class XcreasEntity(CoordinatorEntity["XcreasCoordinator"]):
    """Base entity: per-device registry identity + cloud availability.

    unique_id = `<device_id>:<kind>` and the device registry identifier is
    `(xcreas, <device_id>)` — the CLOUD device id is the stable identity
    (docs/ha-integration.md §6). It deliberately carries NO account scope:
    a device shared into two accounts is registered ONCE per HA instance,
    and when the current source account loses access the entity keeps its
    identity while another entry takes over the device.
    """

    def __init__(self, coordinator: "XcreasCoordinator", device_id: str, kind: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._kind = kind
        self._attr_unique_id = unique_id_for(device_id, kind)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            model=coordinator.device_model(device_id),
            name=coordinator.device_name(device_id),
        )

    @property
    def device_data(self) -> "XcreasDeviceData | None":
        return self.coordinator.data.devices.get(self._device_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        """The device block must be online AND the last refresh must have
        succeeded (docs/ha-integration.md §6: 整条连接或设备失联显示
        unavailable)."""
        data = self.device_data
        if data is None:
            return False
        if not self.coordinator.last_update_success:
            return False
        return data.online

    async def _command(self, action: str, params: dict | None = None) -> None:
        try:
            await self.coordinator.client.async_send_command(self._device_id, action, params)
        except XcreasApiError as err:
            raise HomeAssistantError(f"xcreas command failed: {err.code}") from err


def make_device_reconciler(
    coordinator: "XcreasCoordinator",
    entry,
    hass,
    async_add_entities,
    owned: dict,
    build_entities,
    device_eligible=None,
):
    """The dynamic device reconcile loop (HA dynamic-devices quality rule).

    Returns a @callback that converges `owned` (unique_id → entity) with the
    coordinator's CURRENT full inventory:

    - adds entities for devices present but not owned (skipping unique_ids
      already owned by ANOTHER config entry — the same device shared into
      two accounts is registered once per instance; when the current source
      loses access its removal lets the other entry pick the device up and
      KEEP the entity identity);
    - removes owned entities whose device left the inventory, then schedules
      the device-registry cleanup of this integration's now-orphaned device
      entries (full inventory = authoritative, §4).

    `build_entities(device_id)` returns the entities for one device;
    `device_eligible(device)` gates per-platform capability (the manual-gear
    select only registers devices advertising supports_fan_level).
    """

    from homeassistant.helpers import entity_registry

    @callback
    def _reconcile(snapshot=None) -> None:
        # The listener form receives the NEW snapshot directly: the coordinator
        # fires its device listeners inside _async_update_data, BEFORE
        # self.data is swapped — reading coordinator.data there would
        # reconcile against the PREVIOUS inventory and lose the change.
        er = entity_registry.async_get(hass)
        if snapshot is not None:
            present = snapshot.devices
        else:
            data = coordinator.data
            present = data.devices if data else {}

        # Remove our entities whose device is gone OR no longer eligible
        # (e.g. the manual-gear select when the cloud stops advertising
        # supports_fan_level — the capability gate loads AND unloads).
        removed_devices: set[str] = set()
        for uid in list(owned):
            entity = owned[uid]
            device = present.get(entity._device_id)
            if device is None or (device_eligible is not None and not device_eligible(device)):
                del owned[uid]
                removed_devices.add(entity._device_id)
                # Entity.async_remove alone only renders the state
                # unavailable — the registry row must be removed explicitly
                # or the entity survives the next reload.
                if entity.entity_id:
                    er.async_remove(entity.entity_id)
                entity.async_remove()

        # Add devices present but not ours (cross-entry dedup via the
        # entity registry).
        to_add = []
        for device_id in sorted(present):
            device = present[device_id]
            if device_eligible is not None and not device_eligible(device):
                continue
            for entity in build_entities(device_id):
                uid = entity.unique_id
                if uid in owned:
                    continue
                reg = er.async_get(uid)
                if reg is not None and reg.config_entry_id not in (None, entry.entry_id):
                    continue
                owned[uid] = entity
                to_add.append(entity)
        if to_add:
            async_add_entities(to_add, update_before_add=True)

        if removed_devices:
            _schedule_registry_cleanup(coordinator, hass, entry, removed_devices)

    return _reconcile


def _schedule_registry_cleanup(coordinator, hass, entry, device_ids: set) -> None:
    """Remove this entry's device-registry rows for removed devices AFTER the
    entity removals land (the registry removal is unconditional, so the
    entities must be gone first — HA stale-devices guidance)."""
    from homeassistant.helpers import device_registry as dr

    async def _cleanup() -> None:
        import asyncio

        await asyncio.sleep(10)  # let async_remove() complete its writes
        registry = dr.async_get(hass)
        data = coordinator.data
        present = set(data.devices) if data else set()
        for registry_entry in list(registry.devices.values()):
            for domain, value in registry_entry.identifiers:
                if domain != DOMAIN or value not in device_ids:
                    continue
                if entry.entry_id not in registry_entry.config_entries:
                    continue
                if value in present:
                    continue  # re-added meanwhile — keep the identity
                registry.async_remove_device(registry_entry.id)

    entry.async_create_background_task(hass, _cleanup(), name="xcreas-device-cleanup")
