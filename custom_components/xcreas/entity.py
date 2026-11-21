"""Shared entity helpers (docs/ha-integration.md §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import XcreasApiError
from .const import DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from .coordinator import XcreasCoordinator, XcreasDeviceData


class XcreasEntity(CoordinatorEntity["XcreasCoordinator"]):
    """Base entity: per-device registry identity + cloud availability.

    unique_id = `<account_id>:<device_id>` (+ kind suffix per entity); the
    account scoping keeps two xCREAS accounts inside ONE HA instance apart
    (the config flow already forbids the same device twice — docs/
    ha-integration.md §3).
    """

    def __init__(self, coordinator: "XcreasCoordinator", device_id: str, kind: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._kind = kind
        account = coordinator.account_id
        self._attr_unique_id = f"{account}:{device_id}:{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{account}:{device_id}")},
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
