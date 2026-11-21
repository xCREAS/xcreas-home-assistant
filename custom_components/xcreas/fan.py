"""The xCREAS fan entity (docs/ha-integration.md §5/§6).

Mapping decisions frozen by the task spec (§4 实体映射):

- power: `is_on` judges ONLY the reported power (an ECO-standby motor stop
  must not read as "off");
- speed: percentage 1..100 maps through the server's PercentToRPM table;
  the DISPLAYED percentage comes from the reported setpoint and is shown
  only when the projection is EXACT (percent_approximate=false) — 缺少依据
  时留空;
- presets: auto/sleep/eco via mode control; a manual speed clears the
  preset display by device report, never optimistically;
- NO optimistic state: entities render device reports; the command's
  terminal state only raises errors.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.percentage import ranged_value_to_percentage

from .api import XcreasApiError
from .const import PRESET_MODES, PRESET_TO_MODE, SPEED_COUNT
from .entity import XcreasEntity

_LOGGER = logging.getLogger(__name__)

# The 1..100 percentage scale (speed_count=100 → 1% steps).
SPEED_RANGE = (1, SPEED_COUNT)

MODE_TO_PRESET = {v: k for k, v in PRESET_TO_MODE.items()}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = entry.runtime_data
    entities: list[XcreasFan] = []
    for device_id in list(coordinator.data.devices):
        entities.append(XcreasFan(coordinator, device_id))
    async_add_entities(entities, update_before_add=False)


class XcreasFan(XcreasEntity, FanEntity):
    """The purifier's power / speed / preset surface."""

    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
    )
    _attr_speed_count = SPEED_COUNT
    _attr_preset_modes = PRESET_MODES

    _attr_has_entity_name = True
    _attr_name = None  # the fan IS the device's main feature

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "fan")

    @property
    def is_on(self) -> bool | None:
        """Only the reported power decides (ECO standby keeps the fan report
        at 0 without flipping the switch — task spec §4)."""
        power = _int(self.device_data.control.get("power") if self.device_data else None)
        if power is None:
            return None
        return power == 1

    @property
    def percentage(self) -> int | None:
        """The reported setpoint's percent projection, EXACT only."""
        setpoint = self.device_data.control.get("motor_speed_setpoint") if self.device_data else None
        if not setpoint:
            return None
        approx = setpoint.get("percent_approximate")
        if approx is True:
            return None
        percent = setpoint.get("percent")
        if isinstance(percent, bool):
            return None
        if isinstance(percent, (int, float)) and 1 <= percent <= 100:
            return int(percent)
        # A raw in-window RPM without a percent projection (legacy value):
        # project over the motor's own RPM window, never the percent scale.
        rpm = _int(setpoint)
        if rpm is None:
            return None
        if not (248 <= rpm <= 800):
            return None
        return ranged_value_to_percentage((248, 800), rpm)

    @property
    def preset_mode(self) -> str | None:
        mode = self.device_data.control.get("mode") if self.device_data else None
        if not mode:
            return None
        value = mode.get("value") if isinstance(mode, dict) else None
        return value if value in PRESET_MODES else None

    # --- command surface (§5) ---

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if percentage is not None:
            await self._command("turn_on", {"percentage": int(percentage)})
            return
        if preset_mode is not None:
            if preset_mode not in PRESET_MODES:
                raise HomeAssistantError(f"unknown preset {preset_mode}")
            # "开机并设置" rides the merged-start path server-side.
            await self._command("turn_on", {"preset_mode": preset_mode})
            return
        await self._command("power_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._command("power_off")

    async def async_set_percentage(self, percentage: int) -> None:
        await self._command("set_fan_speed", {"percentage": int(percentage)})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in PRESET_MODES:
            raise HomeAssistantError(f"unknown preset {preset_mode}")
        await self._command("set_preset_mode", {"preset": preset_mode})

    async def _command(self, action: str, params: dict | None = None) -> None:
        try:
            await self.coordinator.client.async_send_command(self._device_id, action, params)
        except XcreasApiError as err:
            raise HomeAssistantError(f"xcreas command failed: {err.code}") from err

    @callback
    def _handle_coordinator_update(self) -> None:
        """State converges by device report (no optimistic writes)."""
        super()._handle_coordinator_update()


def _int(prop: Any) -> int | None:
    """The snapshot's property block → int value (raw passthrough ignored)."""
    if isinstance(prop, dict):
        if prop.get("raw") is True:
            return None
        value = prop.get("value")
    else:
        value = prop
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
