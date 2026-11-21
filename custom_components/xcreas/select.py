"""Fixed manual gear selection for xCREAS purifiers."""

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import XcreasEntity, make_device_reconciler

GEAR_LEVELS = {"gear_1": 1, "gear_2": 2, "gear_3": 3}
LEVEL_OPTIONS = {level: option for option, level in GEAR_LEVELS.items()}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = entry.runtime_data
    owned: dict[str, XcreasManualGear] = {}
    reconcile = make_device_reconciler(
        coordinator,
        entry,
        hass,
        async_add_entities,
        owned,
        lambda device_id: [XcreasManualGear(coordinator, device_id)],
        device_eligible=lambda device: device.supports_fan_level,
    )
    reconcile()
    entry.async_on_unload(coordinator.async_add_device_listener(reconcile))


class XcreasManualGear(XcreasEntity, SelectEntity):
    """Select a fixed manual gear on the same device as the fan slider."""

    _attr_has_entity_name = True
    _attr_translation_key = "manual_gear"
    _attr_options = list(GEAR_LEVELS)

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "manual_gear")

    @property
    def current_option(self) -> str | None:
        device = self.device_data
        if device is None:
            return None
        control = device.control

        def value(key):
            prop = control.get(key)
            if not isinstance(prop, dict) or prop.get("raw"):
                return None
            return prop.get("value")

        power = value("power")
        if type(power) is not int or power != 1 or value("mode") != "manual":
            return None
        indicator = value("motor_speed_indicator")
        level = value("fan_level")
        if type(indicator) is not int or indicator != 0 or type(level) is not int:
            return None
        return LEVEL_OPTIONS.get(level)

    async def async_select_option(self, option: str) -> None:
        level = GEAR_LEVELS.get(option)
        if level is None:
            raise HomeAssistantError(f"unknown manual gear {option}")
        await self._command("set_fan_level", {"fan_level": level})
