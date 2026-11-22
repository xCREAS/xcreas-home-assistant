"""The xCREAS environment sensors (docs/ha-integration.md §4/§6).

- pm25: device_class=pm25, µg/m³, measurement;
- temperature: °C, measurement (the device reports integer Celsius);
- humidity: %, measurement — the API already divides humidity_x100 by 100
  EXACTLY ONCE (the HA side never rescales).

Missing/stale semantics (§4): a field absent from the snapshot renders as
unknown (None native_value); a stale value is still REAL data and stays
displayed; zero is a real zero — never rewritten. The unit threshold
documented in docs/ha-integration.md (freshness = 5m devstate StaleAfter) is
the server's classification; this side only distinguishes present/absent.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    PERCENTAGE,
    UnitOfTemperature,
)
from homeassistant.core import callback

from .entity import XcreasEntity, make_device_reconciler

try:  # UnitOfDensity landed after HA 2025.4 (the verified floor)
    from homeassistant.const import UnitOfDensity
except ImportError:  # pragma: no cover - old-HA path
    UnitOfDensity = None

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = entry.runtime_data

    def build(device_id: str) -> list[XcreasSensor]:
        return [
            XcreasPM25Sensor(coordinator, device_id),
            XcreasTemperatureSensor(coordinator, device_id),
            XcreasHumiditySensor(coordinator, device_id),
        ]

    owned: dict[str, XcreasSensor] = {}
    reconcile = make_device_reconciler(coordinator, entry, hass, async_add_entities, owned, build)
    reconcile()
    entry.async_on_unload(coordinator.async_add_device_listener(reconcile))


class XcreasSensor(XcreasEntity, SensorEntity):
    """Base sensor: has_entity_name + per-kind translation key."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device_id: str, kind: str) -> None:
        super().__init__(coordinator, device_id, kind)
        self._attr_translation_key = kind

    @property
    def native_value(self) -> float | int | None:
        data = self.device_data
        if data is None:
            return None
        prop = data.sensors.get(self._kind)
        if not isinstance(prop, dict):
            return None  # missing field = unknown (never zero-fabricated)
        if prop.get("raw") is True:
            return None
        value = prop.get("value")
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()


class XcreasPM25Sensor(XcreasSensor):
    _attr_device_class = SensorDeviceClass.PM25
    _attr_native_unit_of_measurement = (
        UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
        if UnitOfDensity is not None
        else CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    )

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "pm25")


class XcreasTemperatureSensor(XcreasSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "temperature")


class XcreasHumiditySensor(XcreasSensor):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "humidity")

