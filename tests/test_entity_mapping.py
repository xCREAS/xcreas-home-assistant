"""Entity mapping tests (docs/ha-integration.md §6): the fan power/preset/
percentage semantics and the three sensors' units + unknown/zero/offline
behavior, driven by the shared contract samples."""

from __future__ import annotations

from custom_components.xcreas.const import AVAILABILITY_OFFLINE, AVAILABILITY_ONLINE
from custom_components.xcreas.coordinator import XcreasDeviceData, XcreasSnapshot


def device_from(contract, key: str = "snapshot", index: int = 0) -> XcreasDeviceData:
    raw = contract[key]["devices"][index]
    return XcreasDeviceData(raw["device_id"], raw)


class _FakeCoordinator:
    """The slice of the coordinator the entities read."""

    def __init__(self, contract) -> None:
        self.account_id = contract["snapshot"]["account_id"]
        self.data = XcreasSnapshot(account_id=self.account_id)
        self.data.devices["V350s-001"] = device_from(contract)
        self.data.devices["V350s-002"] = device_from(contract, "snapshot_offline_missing")
        self.last_update_success = True

    def device_data(self, device_id):
        return self.data.devices.get(device_id)

    def device_name(self, device_id):
        dev = self.device_data(device_id)
        return dev.name if dev else None

    def device_model(self, device_id):
        dev = self.device_data(device_id)
        return dev.model if dev else "V350s"


def _coordinator(contract) -> _FakeCoordinator:
    return _FakeCoordinator(contract)


def _fan(contract, device_id="V350s-001"):
    from custom_components.xcreas.fan import XcreasFan

    return XcreasFan(_coordinator(contract), device_id)


def _pm25(contract, device_id="V350s-001"):
    from custom_components.xcreas.sensor import XcreasPM25Sensor

    return XcreasPM25Sensor(_coordinator(contract), device_id)


# --- fan semantics (§6) ---


def test_fan_is_on_follows_power_only(contract) -> None:
    """is_on judges ONLY the reported power — an ECO standby's fan=0 must
    not read as off (task spec §4: eco 待机时依据 power 判断)."""
    fan = _fan(contract)
    assert fan.is_on is True  # power=1 in the sample

    dev = fan.device_data
    dev.raw["control"]["power"]["value"] = 0
    assert fan.is_on is False

    del dev.raw["control"]["power"]
    assert fan.is_on is None  # missing power = unknown, never fabricated off


def test_fan_percentage_exact_projection_only(contract) -> None:
    """§4: 手动风速按实际回报的设定值投影；缺少依据时留空."""
    fan = _fan(contract)
    # The sample's setpoint: 520 RPM, exact 50%.
    setpoint = fan.device_data.control["motor_speed_setpoint"]
    assert setpoint["percent"] == 50
    assert setpoint["percent_approximate"] is False
    assert fan.percentage == 50

    dev = fan.device_data
    dev.raw["control"]["motor_speed_setpoint"]["percent_approximate"] = True
    assert fan.percentage is None  # approximate → hidden (缺少依据时留空)

    del dev.raw["control"]["motor_speed_setpoint"]
    assert fan.percentage is None

    # A legacy approximate projection never displays a percentage.
    dev.raw["control"]["motor_speed_setpoint"] = {
        "value": 652,
        "percent": 73,
        "percent_approximate": True,
    }
    assert fan.percentage is None


def test_fan_preset_mode(contract) -> None:
    fan = _fan(contract)
    dev = fan.device_data
    dev.raw["control"]["mode"] = {"value": "sleep"}
    assert fan.preset_mode == "sleep"
    dev.raw["control"]["mode"] = {"value": "manual"}
    assert fan.preset_mode is None
    dev.raw["control"]["mode"] = {"value": 9, "raw": True}
    assert fan.preset_mode is None
    dev.raw["control"]["mode"] = {"value": "eco"}
    assert fan.preset_mode == "eco"


def test_fan_supported_features(contract) -> None:
    from homeassistant.components.fan import FanEntityFeature

    fan = _fan(contract)
    features = fan.supported_features
    for flag in (
        FanEntityFeature.TURN_ON,
        FanEntityFeature.TURN_OFF,
        FanEntityFeature.SET_SPEED,
        FanEntityFeature.PRESET_MODE,
    ):
        assert features & flag
    assert fan.speed_count == 100
    assert fan.preset_modes == ["auto", "sleep", "eco"]


def test_fan_command_actions(contract) -> None:
    """§5: entity actions map onto the documented API actions."""
    from custom_components.xcreas.const import COMMAND_PATH
    from custom_components.xcreas.fan import XcreasFan
    from tests.conftest import make_client

    calls: list[tuple[str, dict | None]] = []

    class CapturingFan(XcreasFan):
        async def _command(self, action, params=None):
            calls.append((action, params))

    fan = CapturingFan(_coordinator(contract), "V350s-001")
    import asyncio

    async def drive():
        await fan.async_turn_off()
        await fan.async_turn_on()
        await fan.async_turn_on(percentage=0)
        await fan.async_turn_on(percentage=50)
        await fan.async_turn_on(preset_mode="sleep")
        await fan.async_set_percentage(0)
        await fan.async_set_percentage(50)
        await fan.async_set_preset_mode("eco")

    asyncio.run(drive())
    assert calls == [
        ("power_off", None),
        ("power_on", None),
        ("turn_on", {"percentage": 0}),
        ("turn_on", {"percentage": 50}),
        ("turn_on", {"preset_mode": "sleep"}),
        ("set_fan_speed", {"percentage": 0}),
        ("set_fan_speed", {"percentage": 50}),
        ("set_preset_mode", {"preset": "eco"}),
    ]
    _ = COMMAND_PATH, make_client


def test_manual_gear_state_follows_reported_manual_subtype(contract) -> None:
    from custom_components.xcreas.fan import XcreasFan
    from custom_components.xcreas.select import XcreasManualGear

    coordinator = _coordinator(contract)
    gear = XcreasManualGear(coordinator, "V350s-001")
    fan = XcreasFan(coordinator, "V350s-001")
    control = gear.device_data.raw["control"]
    control["fan_level"] = {"value": 2}
    assert gear.current_option is None  # stepless, even if fan_level is set
    assert fan.percentage == 50

    control["motor_speed_indicator"] = {"value": 0}
    assert gear.current_option == "gear_2"
    assert fan.percentage is None  # ignore the old stepless setpoint

    control["mode"] = {"value": "eco"}
    assert gear.current_option is None
    control["mode"] = {"value": "manual"}
    control["power"] = {"value": 0}
    assert gear.current_option is None
    control["power"] = {"value": 1}
    del control["motor_speed_indicator"]
    assert gear.current_option is None


# --- sensor semantics (§6) ---


def test_pm25_sensor(contract) -> None:
    from homeassistant.const import CONCENTRATION_MICROGRAMS_PER_CUBIC_METER

    s = _pm25(contract)
    assert s.native_value == 12
    assert s.native_unit_of_measurement == CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    assert s.unique_id.endswith(":pm25")
    assert s.device_class is not None


def test_temperature_sensor(contract) -> None:
    from homeassistant.const import UnitOfTemperature

    from custom_components.xcreas.sensor import XcreasTemperatureSensor

    s = XcreasTemperatureSensor(_coordinator(contract), "V350s-001")
    assert s.native_value == 23
    assert s.native_unit_of_measurement == UnitOfTemperature.CELSIUS


def test_humidity_sensor_reads_converted_value(contract) -> None:
    """§4: the API converted humidity_x100 once — the HA side reads 47.5
    verbatim and NEVER rescales."""
    from homeassistant.const import PERCENTAGE

    from custom_components.xcreas.sensor import XcreasHumiditySensor

    s = XcreasHumiditySensor(_coordinator(contract), "V350s-001")
    assert s.native_value == 47.5
    assert s.native_unit_of_measurement == PERCENTAGE


def test_missing_sensor_field_is_unknown(contract) -> None:
    """§6: 设备在线但单字段尚无有效数据时该字段为 unknown (None)."""
    s = _pm25(contract, device_id="V350s-002")
    assert s.native_value is None


def test_real_zero_passes_through(contract) -> None:
    """§4: 真实零值不是缺失 — a reported 0 stays 0."""
    s = _pm25(contract)
    s.device_data.raw["sensors"]["pm25"]["value"] = 0
    assert s.native_value == 0


# --- availability (§6) ---


def test_offline_device_unavailable(contract) -> None:
    """§6: 设备失联显示 unavailable。"""
    fan = _fan(contract, device_id="V350s-002")
    sensor = _pm25(contract, device_id="V350s-002")
    assert fan.available is False
    assert sensor.available is False
    assert _coordinator(contract).device_data("V350s-002").availability == AVAILABILITY_OFFLINE


def test_online_device_available(contract) -> None:
    fan = _fan(contract)
    sensor = _pm25(contract)
    assert fan.available is True
    assert sensor.available is True
    assert _coordinator(contract).device_data("V350s-001").availability == AVAILABILITY_ONLINE


def test_failed_refresh_unavailable(contract) -> None:
    """§6: 整条连接失联 → unavailable even if the device block says online."""
    c = _coordinator(contract)
    c.last_update_success = False
    from custom_components.xcreas.fan import XcreasFan
    from custom_components.xcreas.sensor import XcreasPM25Sensor

    assert XcreasFan(c, "V350s-001").available is False
    assert XcreasPM25Sensor(c, "V350s-001").available is False
