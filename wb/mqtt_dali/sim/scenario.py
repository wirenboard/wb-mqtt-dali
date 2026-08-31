"""A DALI installation described as plain data.

A scenario names WB-DALI modules, the control gear and DALI-2 devices on
their buses, and (for the simulator) how the bus is paced. It is JSON-shaped
on purpose — no classes — so it can come from a file, a test, or another
process. `build_network` turns it into a `SimulatedModbusNetwork`;
`serial_config` derives the wb-mqtt-serial config that makes the daemon
discover the same modules.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .control_gear import SimulatedControlDevice, SimulatedControlGear
from .dali_bus import SimulatedDaliBus
from .network import SimulatedModbusNetwork

BUS_NUMBERS = (1, 2, 3)


def default_scenario() -> Dict[str, Any]:
    """One module with a small mixed installation on bus 1.

    Two luminaires already carry short addresses, as they would after a previous
    commissioning run; two are factory-fresh and only appear after a scan. The
    wall switch is a DALI-2 control device, so a scan exercises the input-device
    half of commissioning as well.
    """
    return {
        "gateways": [
            {
                "id": "wb-mdali_1",
                "slaveId": 1,
                "buses": {
                    "1": {
                        "gear": [
                            {"shortAddress": 0, "randomAddress": 0x1A2B3C, "deviceTypes": [6]},
                            {"shortAddress": 1, "randomAddress": 0x4D5E6F, "deviceTypes": [6]},
                            {"shortAddress": None, "randomAddress": 0x7A8B9C, "deviceTypes": [8]},
                            {"shortAddress": None, "randomAddress": 0xC1D2E3, "deviceTypes": [6]},
                        ],
                        "devices": [
                            # Presses itself now and then: without traffic the
                            # daemon did not originate, the bus monitor has
                            # nothing to show.
                            {
                                "shortAddress": None,
                                "randomAddress": 0x2B3C4D,
                                "pressIntervalSeconds": 3,
                            },
                        ],
                    },
                    "2": {"gear": [], "devices": []},
                    "3": {"gear": [], "devices": []},
                },
            }
        ],
        "frameDelaySeconds": 0.0,
    }


def build_network(scenario: Dict[str, Any]) -> SimulatedModbusNetwork:
    """Build the simulated network the scenario describes, self-pressing switches included."""
    network = SimulatedModbusNetwork(frame_delay_s=float(scenario.get("frameDelaySeconds") or 0.0))
    for gateway in scenario.get("gateways", []):
        buses = {index: SimulatedDaliBus() for index in BUS_NUMBERS}
        for bus_key, wiring in (gateway.get("buses") or {}).items():
            bus = buses[_bus_number(bus_key)]
            for unit in _control_gear(wiring):
                bus.add_gear(_make_gear(unit))
            for unit in _control_devices(wiring):
                bus.add_device(_make_device(unit))
        network.add_module(gateway["id"], buses)
        _schedule_input_events(network, gateway)
    return network


def _schedule_input_events(network: SimulatedModbusNetwork, gateway: Dict[str, Any]) -> None:
    """Start any simulated switch that presses itself.

    Without traffic from something other than the daemon there is nothing for
    the bus monitor to show — the reply registers only carry answers to frames
    the gateway sent.
    """
    for bus_key, wiring in (gateway.get("buses") or {}).items():
        for index, unit in enumerate(_control_devices(wiring)):
            interval = unit.get("pressIntervalSeconds")
            if interval:
                network.schedule_button_presses(gateway["id"], _bus_number(bus_key), index, float(interval))


def _bus_number(bus_key: Any) -> int:
    number = int(bus_key)
    if number not in BUS_NUMBERS:
        raise ValueError(f"a WB-DALI module has buses 1..3, not {number}")
    return number


def _control_gear(wiring: Any) -> list:
    """A bus is either a bare list of control gear, or `{gear, devices}`.

    The bare list is what a scenario written before DALI-2 support looks like,
    and one may still be sitting in a browser's local storage.
    """
    if isinstance(wiring, dict):
        return list(wiring.get("gear") or [])
    return list(wiring or [])


def _control_devices(wiring: Any) -> list:
    return list(wiring.get("devices") or []) if isinstance(wiring, dict) else []


def _make_gear(unit: Dict[str, Any]) -> SimulatedControlGear:
    return SimulatedControlGear(
        shortaddr=unit.get("shortAddress"),
        random_address=unit.get("randomAddress"),
        colour_temperature=unit.get("colourTemperature"),
        groups=set(unit.get("groups") or []),
        devicetypes=list(unit.get("deviceTypes") or []),
    )


def _make_device(unit: Dict[str, Any]) -> SimulatedControlDevice:
    return SimulatedControlDevice(
        shortaddr=unit.get("shortAddress"),
        random_address=unit.get("randomAddress"),
    )


DEFAULT_SERIAL_SETTINGS = {"baud_rate": 9600, "data_bits": 8, "parity": "N", "stop_bits": 2}


def serial_settings(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """The RS-485 line settings for hardware mode.

    A WB-DALI module is not necessarily at the factory 9600 8N2, and there is no
    autodetection here — the Modbus editor's scan is where a port gets probed.
    """
    return {**DEFAULT_SERIAL_SETTINGS, **(scenario.get("serialSettings") or {})}


def slave_ids(scenario: Dict[str, Any]) -> Dict[str, int]:
    """Module id → Modbus slave id, defaulting to the module's position in the scenario."""
    return {
        gateway["id"]: gateway.get("slaveId", index + 1)
        for index, gateway in enumerate(scenario.get("gateways", []))
    }


def serial_config(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """The wb-mqtt-serial config the daemon reads to find WB-DALI modules.

    `Gateway._update_gateways` drops any gateway that is not listed here as an
    enabled WB-DALI device, so the two descriptions have to agree.
    """
    devices: List[Dict[str, Any]] = [
        {"id": device_id, "slave_id": slave_id, "device_type": "WB-DALI", "enabled": True}
        for device_id, slave_id in slave_ids(scenario).items()
    ]
    return {"ports": [{"path": "/dev/ttyRS485-1", "enabled": True, "devices": devices}]}
